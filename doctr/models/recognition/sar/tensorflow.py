# Copyright (C) 2021, Mindee.

# This program is licensed under the Apache License version 2.
# See LICENSE or go to <https://www.apache.org/licenses/LICENSE-2.0.txt> for full license details.

from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from official.nlp.modeling.ops import beam_search
import tensorflow as tf
from tensorflow.keras import Model, Sequential, layers

from doctr.utils.repr import NestedObject

from ....datasets import VOCABS
from ...backbones import resnet31
from ...utils import load_pretrained_params
from ..core import RecognitionModel, RecognitionPostProcessor

# __all__ = ["SAR", "SARPostProcessor", "sar_resnet31"]
__all__ = [
    "SAR",
    "SAR_without_feature_extractor",
    "SAR_feature_extractor_with_lstm_encoder",
    "SAR_without_lstm_encoder",
    "SARPostProcessor",
    "sar_resnet31",
]

default_cfgs: Dict[str, Dict[str, Any]] = {
    "sar_resnet31": {
        "mean": (0.694, 0.695, 0.693),
        "std": (0.299, 0.296, 0.301),
        "backbone": resnet31,
        "rnn_units": 512,
        "max_length": 30,
        "num_decoders": 2,
        "input_shape": (32, 128, 3),
        "vocab": VOCABS["legacy_french"],
        "url": "https://github.com/mindee/doctr/releases/download/v0.3.0/sar_resnet31-9ee49970.zip",
    },
}


class AttentionModule(layers.Layer, NestedObject):
    """Implements attention module of the SAR model

    Args:
        attention_units: number of hidden attention units

    """

    def __init__(self, attention_units: int) -> None:

        super().__init__()
        self.hidden_state_projector = layers.Conv2D(
            attention_units,
            1,
            strides=1,
            use_bias=False,
            padding="same",
            kernel_initializer="he_normal",
        )
        self.features_projector = layers.Conv2D(
            attention_units,
            3,
            strides=1,
            use_bias=True,
            padding="same",
            kernel_initializer="he_normal",
        )
        self.attention_projector = layers.Conv2D(
            1,
            1,
            strides=1,
            use_bias=False,
            padding="same",
            kernel_initializer="he_normal",
        )
        self.flatten = layers.Flatten()

    def call(
        self,
        features: tf.Tensor,
        hidden_state: tf.Tensor,
        **kwargs: Any,
    ) -> tf.Tensor:

        features_dims = [tf.shape(features)[k] for k in range(len(features.shape))]
        # [H, W] = features.get_shape().as_list()[1:3]
        # shape (N, 1, 1, rnn_units) -> (N, 1, 1, attention_units)
        hidden_state_projection = self.hidden_state_projector(hidden_state, **kwargs)
        # shape (N, H, W, D) -> (N, H, W, attention_units)
        features_projection = self.features_projector(features, **kwargs)
        # shape (N, 1, 1, attention_units) + (N, H, W, attention_units) -> (N, H, W, attention_units)
        projection = tf.math.tanh(hidden_state_projection + features_projection)
        # shape (N, H, W, attention_units) -> (N, H, W, 1)
        attention = self.attention_projector(projection, **kwargs)
        # shape (N, H, W, 1) -> (N, H * W)
        attention_shape = [tf.shape(attention)[k] for k in range(len(attention.shape))]

        attention = tf.reshape(attention, attention_shape[:-3] + [features_dims[1] * features_dims[2]])
        attention = tf.nn.softmax(attention)
        # shape (N, H * W) -> (N, H, W, 1)
        attention_map = tf.reshape(attention, attention_shape)
        # shape (N, H, W, D) x (N, H, W, 1) -> (N, H, W, D)
        glimpse = tf.math.multiply(features, attention_map)
        # shape (N, H, W, D) -> (N, D)
        glimpse = tf.reduce_sum(glimpse, axis=[-3, -2])
        return glimpse


class SARDecoder(layers.Layer, NestedObject):
    """Implements decoder module of the SAR model

    Args:
        rnn_units: number of hidden units in recurrent cells
        max_length: maximum length of a sequence
        vocab_size: number of classes in the model alphabet
        embedding_units: number of hidden embedding units
        attention_units: number of hidden attention units
        num_decoder_layers: number of LSTM layers to stack

    """

    def __init__(
        self,
        rnn_units: int,
        max_length: int,
        vocab_size: int,
        attention_units: int,
        num_decoder_layers: int = 2,
        input_shape: Optional[List[Tuple[Optional[int]]]] = None,
    ) -> None:

        super().__init__()
        self.vocab_size = vocab_size
        self.lstm_decoder = layers.StackedRNNCells(
            [
                layers.LSTMCell(rnn_units, implementation=1)
                for _ in range(num_decoder_layers)
            ]
        )
        self.embed = layers.Dense(
            rnn_units, use_bias=False, input_shape=(None, self.vocab_size + 1)
        )
        self.attention_module = AttentionModule(attention_units)
        self.output_dense = layers.Dense(vocab_size + 1, use_bias=True)
        self.max_length = max_length

        # Initialize kernels
        if input_shape is not None:
            self.attention_module.call(
                layers.Input(input_shape[0][1:]), layers.Input((1, 1, rnn_units))
            )

    def embed_token(self, token: tf.Tensor, **kwargs):
        return self.embed(tf.one_hot(token, depth=self.vocab_size + 1), **kwargs)

    def greedy_decode(self, symbol, states, features, gt, **kwargs):
        logits_list = []

        for t in range(self.max_length + 1):  # keep 1 step for <eos>
            # one-hot symbol with depth vocab_size + 1
            # embeded_symbol: shape (N, embedding_units)
            embeded_symbol = self.embed(
                tf.one_hot(symbol, depth=self.vocab_size + 1), **kwargs
            )
            logits, states = self.lstm_decoder(embeded_symbol, states, **kwargs)
            glimpse = self.attention_module(
                features,
                tf.expand_dims(tf.expand_dims(logits, axis=1), axis=1),
                **kwargs,
            )
            # logits: shape (N, rnn_units), glimpse: shape (N, 1)
            logits = tf.concat([logits, glimpse], axis=-1)
            # shape (N, rnn_units + 1) -> (N, vocab_size + 1)
            logits = self.output_dense(logits, **kwargs)
            # update symbol with predicted logits for t+1 step
            if gt is not None:
                # Teacher forcing
                symbol = gt[:, t]  # type: ignore[index]
            else:
                symbol = tf.argmax(logits, axis=-1)
            logits_list.append(logits)

            scores = tf.stack(logits_list, axis=1)
            ids = tf.math.argmax(scores, axis=-1)

        return ids, scores

    def beam_decode(self, symbol, states, features, beam_width, **kwargs):
        cache = {"hidden_states": states, "features": features}

        # Sequence ids : shape (N, beam_width, seq_length)
        # Sequence log-probabilities : shape (N, beam_width)
        sequences_ids, sequences_log_probs = beam_search.sequence_beam_search(
            symbols_to_logits_fn=self.get_symbols_to_logits_fn(),
            initial_ids=symbol,
            initial_cache=cache,
            vocab_size=self.vocab_size + 1,
            beam_size=beam_width,
            alpha=0,
            max_decode_length=self.max_length + 1,
            eos_id=self.vocab_size,
            padded_decode=False,
            dtype="float32",
        )
        sequences_ids = sequences_ids[..., 1:]
        return sequences_ids, sequences_log_probs

    def get_symbols_to_logits_fn(self):
        def symbols_to_logits_fn(ids, i, cache):
            embeded_symbol = self.embed_token(ids[:, -1])
            output, states = self.lstm_decoder(embeded_symbol, cache["hidden_states"])
            cache["hidden_states"] = states

            # glimpse: shape (N, H, W, D) | (N, 1, 1, rnn_units) -> (N, D)
            glimpse = self.attention_module(
                cache["features"],
                tf.expand_dims(tf.expand_dims(output, axis=-2), axis=-2),
            )
            # logits: shape [output (N, rnn_units), glimpse (N, D)] -> (N, rnn_units + D)
            logits = tf.concat([output, glimpse], axis=-1)
            # shape (N, rnn_units + D) -> (N, vocab_size + 1)
            logits = self.output_dense(logits)

            return logits, cache

        return symbols_to_logits_fn

    def call(
        self,
        features: tf.Tensor,
        holistic: tf.Tensor,
        gt: Optional[tf.Tensor] = None,
        beam_width: Optional[int] = None,
        **kwargs: Any,
    ) -> tf.Tensor:

        # initialize states (each of shape (N, rnn_units))
        states = self.lstm_decoder.get_initial_state(
            inputs=None, batch_size=tf.shape(features)[0], dtype=features.dtype
        )
        # run first step of lstm
        # holistic: shape (N, rnn_units)
        _, states = self.lstm_decoder(holistic, states, **kwargs)
        # Initialize with the index of virtual START symbol (placed after <eos> so that the one-hot is only zeros)
        symbol = tf.fill((tf.shape(features)[0],), self.vocab_size + 1)

        if kwargs.get("training") or beam_width is None:
            # Scores here are logits: shape (N, max_length + 1, vocab_size + 1)
            ids, scores = self.greedy_decode(
                symbol=symbol, states=states, gt=gt, features=features, **kwargs
            )
        else:
            # Scores here are probabilities: shape (N, beam_width, seq_length, vocab_size + 1)
            ids, scores = self.beam_decode(
                symbol=symbol,
                states=states,
                features=features,
                beam_width=beam_width,
                **kwargs,
            )

        return ids, scores


class SARCellOutputLayer(layers.Layer):
    def __init__(
        self,
        attention_module: AttentionModule,
        output_dense: layers.Dense,
        feature_map: tf.Tensor,
        **kwargs,
    ):
        self.attention_module = attention_module
        self.output_dense = output_dense
        self.feature_map = feature_map
        super(SARCellOutputLayer, self).__init__(**kwargs)

    def call(self, inputs: tf.Tensor, **kwargs):
        # inputs: shape (N, B, rnn_units) -> (B, N, rnn_units)
        inputs = tf.transpose(inputs, (1, 0, 2))
        # glimpse: shape (N, H, W, D) | (B, N, 1, 1, rnn_units) -> (B, N, D)
        glimpse = self.attention_module(
            self.feature_map,
            tf.expand_dims(tf.expand_dims(inputs, axis=-2), axis=-2),
            **kwargs,
        )
        # logits: shape [inputs (B, N, rnn_units), glimpse (B, N, D)] -> (B, N, rnn_units + D)
        logits = tf.concat([inputs, glimpse], axis=-1)
        # shape (B, N, rnn_units + D) -> (B, N, vocab_size + 1)
        logits = self.output_dense(logits, **kwargs)
        # shape (B, N, vocab_size + 1) -> (N, B, vocab_size + 1)
        logits = tf.transpose(logits, (1, 0, 2))

        return logits

    def compute_output_shape(self, input_shape: tf.Tensor):
        return self.output_dense.compute_output_shape(input_shape)


class SAR(Model, RecognitionModel):
    """Implements a SAR architecture as described in `"Show, Attend and Read:A Simple and Strong Baseline for
    Irregular Text Recognition" <https://arxiv.org/pdf/1811.00751.pdf>`_.

    Args:
        feature_extractor: the backbone serving as feature extractor
        vocab: vocabulary used for encoding
        rnn_units: number of hidden units in both encoder and decoder LSTM
        embedding_units: number of embedding units
        attention_units: number of hidden units in attention module
        max_length: maximum word length handled by the model
        num_decoders: number of LSTM to stack in decoder layer

    """

    _children_names: List[str] = [
        "feat_extractor",
        "encoder",
        "decoder",
        "postprocessor",
    ]

    def __init__(
        self,
        feature_extractor,
        vocab: str,
        rnn_units: int = 512,
        attention_units: int = 512,
        max_length: int = 30,
        num_decoders: int = 2,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:

        super().__init__()
        self.vocab = vocab
        self.cfg = cfg

        self.max_length = (
            max_length + 1
        )  # Add 1 timestep for EOS after the longest word

        self.feat_extractor = feature_extractor

        self.encoder = Sequential(
            [
                layers.LSTM(units=rnn_units, return_sequences=True),
                layers.LSTM(units=rnn_units, return_sequences=False),
            ]
        )
        # Initialize the kernels (watch out for reduce_max)
        self.encoder.build(input_shape=(None,) + self.feat_extractor.output_shape[2:])

        self.decoder = SARDecoder(
            rnn_units=rnn_units,
            max_length=max_length,
            vocab_size=len(vocab),
            attention_units=attention_units,
            num_decoder_layers=num_decoders,
        )

        self.postprocessor = SARPostProcessor(vocab=vocab)

    @staticmethod
    def compute_loss(
        model_output: tf.Tensor,
        gt: tf.Tensor,
        seq_len: tf.Tensor,
        from_logits: bool = True,
        **kwargs,
    ) -> tf.Tensor:
        """Compute categorical cross-entropy loss for the model.
        Sequences are masked after the EOS character.

        Args:
            gt: the encoded tensor with gt labels
            model_output: predicted logits of the model
            seq_len: lengths of each gt word inside the batch
            from_logits: Whether model_output is expected to be a logits tensor. By default, we assume that
            model_output encodes logits.

        Returns:
            The loss of the model on the batch
        """
        # Input length : number of timesteps
        input_len = tf.shape(model_output)[-2]
        # Add one for additional <eos> token
        seq_len = seq_len + 1
        # One-hot gt labels
        oh_gt = tf.one_hot(gt, depth=model_output.shape[-1])

        if from_logits:
            # Label smoothing
            if kwargs.get("conf_matrix") is not None:
                oh_gt = tf.matmul(oh_gt, kwargs.get("conf_matrix"))
            # Compute loss
            cce = tf.nn.softmax_cross_entropy_with_logits(oh_gt, model_output)

        else:
            # Take the top sequence
            model_output = model_output[:, 0, ...]
            # Convert scores from log probabilities to probabilities
            model_output = tf.math.exp(model_output)
            # Compute loss
            loss = tf.keras.losses.CategoricalCrossentropy(
                from_logits=False, reduction="none"
            )
            cce = loss(oh_gt[:, :input_len], model_output[:, :input_len])

        # Compute mask
        mask_values = tf.zeros_like(cce)
        mask_2d = tf.sequence_mask(seq_len, input_len)
        masked_loss = tf.where(mask_2d, cce, mask_values)
        ce_loss = tf.math.divide(
            tf.reduce_sum(masked_loss, axis=1), tf.cast(seq_len, model_output.dtype)
        )
        return tf.expand_dims(ce_loss, axis=1)

    def call(
        self,
        x: tf.Tensor,
        target: Optional[List[str]] = None,
        return_model_output: bool = False,
        return_preds: bool = False,
        beam_width: Optional[int] = None,
        top_sequences: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Feature extraction: shape (N, H, W, D)
        features = self.feat_extractor(x, **kwargs)
        # Vertical max pooling: shape (N, H, W, D) -> (N, W, D)
        pooled_features = tf.reduce_max(features, axis=1)
        # Bilayer LSTM Encoder: shape (N, W, D) -> (N, rnn_units)
        encoded = self.encoder(pooled_features, **kwargs)
        # Encode target
        if target is not None:
            gt, seq_len = self.compute_target(target)
            seq_len = tf.cast(seq_len, tf.int32)
        # Bilayer LSTM with attention decoder
        decoded_ids, decoded_scores = self.decoder(
            features=features,
            holistic=encoded,
            gt=None if target is None else gt,
            beam_width=beam_width,
            **kwargs,
        )

        out: Dict[str, tf.Tensor] = {}

        if return_model_output:
            out["out_map"] = decoded_scores

        if target is None or return_preds:
            # Post-process boxes
            out["preds"] = self.postprocessor(
                scores=decoded_scores, ids=decoded_ids, top_sequences=top_sequences
            )

        if target is not None:
            out["loss"] = self.compute_loss(
                decoded_scores,
                gt,
                seq_len,
                from_logits=kwargs.get("training") or beam_width is None,
                **kwargs,
            )

        return out


class SAR_feature_extractor_with_lstm_encoder(Model, RecognitionModel):
    """Implements a SAR architecture as described in `"Show, Attend and Read:A Simple and Strong Baseline for
    Irregular Text Recognition" <https://arxiv.org/pdf/1811.00751.pdf>`_.

    Args:
        feature_extractor: the backbone serving as feature extractor
        vocab: vocabulary used for encoding
        rnn_units: number of hidden units in both encoder and decoder LSTM
        embedding_units: number of embedding units
        attention_units: number of hidden units in attention module
        max_length: maximum word length handled by the model
        num_decoders: number of LSTM to stack in decoder layer

    """

    _children_names: List[str] = ["feat_extractor", "encoder"]

    def __init__(
        self,
        feature_extractor,
        rnn_units: int = 512,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:

        super().__init__()
        self.cfg = cfg

        self.feat_extractor = feature_extractor

        self.encoder = Sequential(
            [
                layers.LSTM(units=rnn_units, return_sequences=True),
                layers.LSTM(units=rnn_units, return_sequences=False),
            ]
        )
        # Initialize the kernels (watch out for reduce_max)
        self.encoder.build(input_shape=(None,) + self.feat_extractor.output_shape[2:])

    def call(
        self,
        x: tf.Tensor,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Feature extraction: shape (N, H, W, D)
        features = self.feat_extractor(x, **kwargs)
        # Vertical max pooling: shape (N, H, W, D) -> (N, W, D)
        pooled_features = tf.reduce_max(features, axis=1)
        # # Bilayer LSTM Encoder: shape (N, W, D) -> (N, rnn_units)
        encoded = self.encoder(pooled_features, **kwargs)

        # # Encode target
        # if target is not None:
        #     gt, seq_len = self.compute_target(target)
        #     seq_len = tf.cast(seq_len, tf.int32)
        # # Bilayer LSTM with attention decoder
        # decoded_features = self.decoder(
        #     features=features, holistic=encoded, gt=None if target is None else gt, beam_width=beam_width, **kwargs
        # )

        out: Dict[str, tf.Tensor] = {}
        out["features"] = features
        out["encoded"] = encoded
        # if return_model_output:
        #     out["out_map"] = decoded_features
        #
        # if target is None or return_preds:
        #     # Post-process boxes
        #     out["preds"] = self.postprocessor(decoded_features, top_sequences=top_sequences)
        #
        # if target is not None:
        #     out["loss"] = self.compute_loss(
        #         decoded_features, gt, seq_len, from_logits=kwargs.get("training") or beam_width is None, **kwargs
        #     )

        return out


class SAR_without_lstm_encoder(Model, RecognitionModel):
    """Implements a SAR architecture as described in `"Show, Attend and Read:A Simple and Strong Baseline for
    Irregular Text Recognition" <https://arxiv.org/pdf/1811.00751.pdf>`_.

    Args:
        feature_extractor: the backbone serving as feature extractor
        vocab: vocabulary used for encoding
        rnn_units: number of hidden units in both encoder and decoder LSTM
        embedding_units: number of embedding units
        attention_units: number of hidden units in attention module
        max_length: maximum word length handled by the model
        num_decoders: number of LSTM to stack in decoder layer

    """

    # _children_names: List[str] = ["feat_extractor", "encoder", "decoder", "postprocessor"]
    _children_names: List[str] = ["decoder", "postprocessor"]

    def __init__(
        self,
        # feature_extractor,
        # feat_extractor_output_shape : Tuple[int],# B x H x W x C
        vocab: str,
        rnn_units: int = 512,
        attention_units: int = 512,
        max_length: int = 30,
        num_decoders: int = 2,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:

        super().__init__()
        self.vocab = vocab
        self.cfg = cfg

        self.max_length = (
            max_length + 1
        )

        self.decoder = SARDecoder(
            rnn_units=rnn_units,
            max_length=max_length,
            vocab_size=len(vocab),
            attention_units=attention_units,
            num_decoder_layers=num_decoders,
        )

        self.postprocessor = SARPostProcessor(vocab=vocab)

    @staticmethod
    def compute_loss(
        model_output: tf.Tensor,
        gt: tf.Tensor,
        seq_len: tf.Tensor,
        from_logits: bool = True,
        **kwargs,
    ) -> tf.Tensor:
        """Compute categorical cross-entropy loss for the model.
        Sequences are masked after the EOS character.

        Args:
            gt: the encoded tensor with gt labels
            model_output: predicted logits of the model
            seq_len: lengths of each gt word inside the batch
            from_logits: Whether model_output is expected to be a logits tensor. By default, we assume that
            model_output encodes logits.

        Returns:
            The loss of the model on the batch
        """
        # Input length : number of timesteps
        input_len = tf.shape(model_output)[-2]
        # Add one for additional <eos> token
        seq_len = seq_len + 1
        # One-hot gt labels
        oh_gt = tf.one_hot(gt, depth=model_output.shape[-1])

        if from_logits:
            # Label smoothing
            if kwargs.get("conf_matrix") is not None:
                oh_gt = tf.matmul(oh_gt, kwargs.get("conf_matrix"))
            # Compute loss
            cce = tf.nn.softmax_cross_entropy_with_logits(oh_gt, model_output)

        else:
            # Take the top sequence
            model_output = model_output[:, 0, ...]
            # Convert scores from log probabilities to probabilities
            model_output = tf.math.exp(model_output)
            # Compute loss
            loss = tf.keras.losses.CategoricalCrossentropy(
                from_logits=False, reduction="none"
            )
            cce = loss(oh_gt[:, :input_len], model_output[:, :input_len])

        # Compute mask
        mask_values = tf.zeros_like(cce)
        mask_2d = tf.sequence_mask(seq_len, input_len)
        masked_loss = tf.where(mask_2d, cce, mask_values)
        ce_loss = tf.math.divide(
            tf.reduce_sum(masked_loss, axis=1), tf.cast(seq_len, model_output.dtype)
        )
        return tf.expand_dims(ce_loss, axis=1)

    def call(
        self,
        # x: tf.Tensor,
        features: tf.Tensor,
        encoded: tf.Tensor,  # encoded features with a lstm
        target: Optional[List[str]] = None,
        return_model_output: bool = False,
        return_preds: bool = False,
        beam_width: Optional[int] = None,
        top_sequences: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Feature extraction: shape (N, H, W, D)
        # features = self.feat_extractor(x, **kwargs)
        # Vertical max pooling: shape (N, H, W, D) -> (N, W, D)

        # pooled_features = tf.reduce_max(features, axis=1)
        # # Bilayer LSTM Encoder: shape (N, W, D) -> (N, rnn_units)
        # encoded = self.encoder(pooled_features, **kwargs)

        # Encode target
        if target is not None:
            gt, seq_len = self.compute_target(target)
            seq_len = tf.cast(seq_len, tf.int32)
        # Bilayer LSTM with attention decoder
        if "conf_matrix" in kwargs:
            label_smoothing_weights = kwargs.pop("conf_matrix")
        else:
            label_smoothing_weights = None
        decoded_features = self.decoder(
            features=features,
            holistic=encoded,
            gt=None if target is None else gt,
            beam_width=beam_width,
            **kwargs,
        )

        out: Dict[str, tf.Tensor] = {}
        if return_model_output:
            out["out_map"] = decoded_features

        if target is None or return_preds:
            # Post-process boxes
            out["preds"] = self.postprocessor(
                decoded_features, top_sequences=top_sequences
            )

        if target is not None:
            kwargs["conf_matrix"] = label_smoothing_weights
            out["loss"] = self.compute_loss(
                decoded_features,
                gt,
                seq_len,
                from_logits=kwargs.get("training") or beam_width is None,
                **kwargs,
            )

        return out


class SAR_without_feature_extractor(Model, RecognitionModel):
    """Implements a SAR architecture as described in `"Show, Attend and Read:A Simple and Strong Baseline for
    Irregular Text Recognition" <https://arxiv.org/pdf/1811.00751.pdf>`_.

    Args:
        feature_extractor: the backbone serving as feature extractor
        vocab: vocabulary used for encoding
        rnn_units: number of hidden units in both encoder and decoder LSTM
        embedding_units: number of embedding units
        attention_units: number of hidden units in attention module
        max_length: maximum word length handled by the model
        num_decoders: number of LSTM to stack in decoder layer

    """

    # _children_names: List[str] = ["feat_extractor", "encoder", "decoder", "postprocessor"]
    _children_names: List[str] = ["encoder", "decoder", "postprocessor"]

    def __init__(
        self,
        # feature_extractor,
        feat_extractor_output_shape: Tuple[int],  # B x H x W x C
        vocab: str,
        rnn_units: int = 512,
        attention_units: int = 512,
        max_length: int = 30,
        num_decoders: int = 2,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:

        super().__init__()
        self.vocab = vocab
        self.cfg = cfg

        self.max_length = (
            max_length + 1
        )  # Add 1 timestep for EOS after the longest word

        # self.feat_extractor = feature_extractor
        self.feat_extractor_output_shape = feat_extractor_output_shape

        self.encoder = Sequential(
            [
                layers.LSTM(units=rnn_units, return_sequences=True),
                layers.LSTM(units=rnn_units, return_sequences=False),
            ]
        )
        # Initialize the kernels (watch out for reduce_max)
        self.encoder.build(input_shape=(None,) + self.feat_extractor_output_shape[2:])

        self.decoder = SARDecoder(
            rnn_units=rnn_units,
            max_length=max_length,
            vocab_size=len(vocab),
            attention_units=attention_units,
            num_decoder_layers=num_decoders,
        )

        self.postprocessor = SARPostProcessor(vocab=vocab)

    @staticmethod
    def compute_loss(
        model_output: tf.Tensor,
        gt: tf.Tensor,
        seq_len: tf.Tensor,
        from_logits: bool = True,
        **kwargs,
    ) -> tf.Tensor:
        """Compute categorical cross-entropy loss for the model.
        Sequences are masked after the EOS character.

        Args:
            gt: the encoded tensor with gt labels
            model_output: predicted logits of the model
            seq_len: lengths of each gt word inside the batch
            from_logits: Whether model_output is expected to be a logits tensor. By default, we assume that
            model_output encodes logits.

        Returns:
            The loss of the model on the batch
        """
        # Input length : number of timesteps
        input_len = tf.shape(model_output)[-2]
        # Add one for additional <eos> token
        seq_len = seq_len + 1
        # One-hot gt labels
        oh_gt = tf.one_hot(gt, depth=model_output.shape[-1])

        if from_logits:
            # Label smoothing
            if kwargs.get("conf_matrix") is not None:
                oh_gt = tf.matmul(oh_gt, kwargs.get("conf_matrix"))
            # Compute loss
            cce = tf.nn.softmax_cross_entropy_with_logits(oh_gt, model_output)

        else:
            # Take the top sequence
            model_output = model_output[:, 0, ...]
            # Convert scores from log probabilities to probabilities
            model_output = tf.math.exp(model_output)
            # Compute loss
            loss = tf.keras.losses.CategoricalCrossentropy(
                from_logits=False, reduction="none"
            )
            cce = loss(oh_gt[:, :input_len], model_output[:, :input_len])

        # Compute mask
        mask_values = tf.zeros_like(cce)
        mask_2d = tf.sequence_mask(seq_len, input_len)
        masked_loss = tf.where(mask_2d, cce, mask_values)
        ce_loss = tf.math.divide(
            tf.reduce_sum(masked_loss, axis=1), tf.cast(seq_len, model_output.dtype)
        )
        return tf.expand_dims(ce_loss, axis=1)

    def call(
        self,
        # x: tf.Tensor,
        features: tf.Tensor,
        target: Optional[List[str]] = None,
        return_model_output: bool = False,
        return_preds: bool = False,
        beam_width: Optional[int] = None,
        top_sequences: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # Feature extraction: shape (N, H, W, D)
        # features = self.feat_extractor(x, **kwargs)

        # Vertical max pooling: shape (N, H, W, D) -> (N, W, D)
        pooled_features = tf.reduce_max(features, axis=1)
        # Bilayer LSTM Encoder: shape (N, W, D) -> (N, rnn_units)
        if "conf_matrix" in kwargs:
            label_smoothing_weights = kwargs.pop("conf_matrix")
        else:
            label_smoothing_weights = None

        encoded = self.encoder(pooled_features, **kwargs)

        # Encode target
        if target is not None:
            gt, seq_len = self.compute_target(target)
            seq_len = tf.cast(seq_len, tf.int32)
        # Bilayer LSTM with attention decoder
        decoded_ids, decoded_scores = self.decoder(
            features=features,
            holistic=encoded,
            gt=None if target is None else gt,
            beam_width=beam_width,
            **kwargs,
        )

        out: Dict[str, tf.Tensor] = {}

        if return_model_output:
            out["out_map"] = decoded_scores

        if target is None or return_preds:
            # Post-process boxes
            out["preds"] = self.postprocessor(
                scores=decoded_scores, ids=decoded_ids, top_sequences=top_sequences
            )

        if target is not None:
            kwargs["conf_matrix"] = label_smoothing_weights
            out["loss"] = self.compute_loss(
                decoded_features,
                gt,
                seq_len,
                from_logits=kwargs.get("training") or beam_width is None,
                **kwargs,
            )

        return out


class SARPostProcessor(RecognitionPostProcessor):
    """Post processor for SAR architectures

    Args:
        vocab: string containing the ordered sequence of supported characters
    """

    def __call__(
        self,
        ids: tf.Tensor,
        scores: tf.Tensor,
        top_sequences: Optional[int] = None,
    ) -> List[Tuple[str, float]]:

        # N x L
        if top_sequences:
            # Score are already sequences log-probabilities
            probs = tf.math.exp(scores)[:, :top_sequences]
        else:
            # Compute sequence probabilities
            probs = tf.nn.softmax(scores, axis=-1)
            probs = tf.gather(probs, ids, axis=-1, batch_dims=2)
            # Take the geometric mean probability of the sequence
            probs = tf.math.exp(tf.math.reduce_mean(tf.math.log(probs), axis=-1))

        # decode raw output of the model with tf_label_to_idx
        ids = tf.cast(ids, dtype="int32")
        embedding = tf.constant(self._embedding, dtype=tf.string)
        decoded_strings_pred = tf.strings.reduce_join(
            inputs=tf.nn.embedding_lookup(embedding, ids), axis=-1
        )
        decoded_strings_pred = tf.strings.split(decoded_strings_pred, "<eos>")
        decoded_strings_pred = tf.sparse.to_dense(
            decoded_strings_pred.to_sparse(), default_value="not valid"
        )[..., 0]

        if top_sequences:
            decoded_strings_pred = decoded_strings_pred[:, :top_sequences]

        return {"predictions": decoded_strings_pred, "probabilities": probs}


def _sar(
    arch: str,
    pretrained: bool,
    pretrained_backbone: bool = True,
    input_shape: Tuple[int, int, int] = None,
    **kwargs: Any,
) -> SAR:

    pretrained_backbone = pretrained_backbone and not pretrained

    # Patch the config
    _cfg = deepcopy(default_cfgs[arch])
    _cfg["input_shape"] = input_shape or _cfg["input_shape"]
    _cfg["vocab"] = kwargs.get("vocab", _cfg["vocab"])
    _cfg["rnn_units"] = kwargs.get("rnn_units", _cfg["rnn_units"])
    _cfg["attention_units"] = kwargs.get("attention_units", _cfg["rnn_units"])
    _cfg["max_length"] = kwargs.get("max_length", _cfg["max_length"])
    _cfg["num_decoders"] = kwargs.get("num_decoders", _cfg["num_decoders"])

    # Feature extractor
    feat_extractor = default_cfgs[arch]["backbone"](
        input_shape=_cfg["input_shape"],
        pretrained=pretrained_backbone,
        include_top=False,
    )

    kwargs["vocab"] = _cfg["vocab"]
    kwargs["rnn_units"] = _cfg["rnn_units"]
    kwargs["attention_units"] = _cfg["attention_units"]
    kwargs["max_length"] = _cfg["max_length"]
    kwargs["num_decoders"] = _cfg["num_decoders"]

    # Build the model
    model = SAR(feat_extractor, cfg=_cfg, **kwargs)
    # Load pretrained parameters
    if pretrained:
        load_pretrained_params(model, default_cfgs[arch]["url"])

    return model


def sar_resnet31(pretrained: bool = False, **kwargs: Any) -> SAR:
    """SAR with a resnet-31 feature extractor as described in `"Show, Attend and Read:A Simple and Strong
    Baseline for Irregular Text Recognition" <https://arxiv.org/pdf/1811.00751.pdf>`_.

    Example:
        >>> import tensorflow as tf
        >>> from doctr.models import sar_resnet31
        >>> model = sar_resnet31(pretrained=False)
        >>> input_tensor = tf.random.uniform(shape=[1, 64, 256, 3], maxval=1, dtype=tf.float32)
        >>> out = model(input_tensor)

    Args:
        pretrained (bool): If True, returns a model pre-trained on our text recognition dataset

    Returns:
        text recognition architecture
    """

    return _sar("sar_resnet31", pretrained, **kwargs)
