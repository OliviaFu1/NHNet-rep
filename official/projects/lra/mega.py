# Copyright 2024 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Mega model configurations and instantiation methods."""
import dataclasses

import tensorflow as tf
import tensorflow.keras as tf_keras

from official.modeling import tf_utils
from official.modeling.hyperparams import base_config
from official.nlp.configs import encoders
from official.projects.lra.mega_encoder import MegaEncoder


@dataclasses.dataclass
class MegaEncoderConfig(encoders.BertEncoderConfig):
  """Extra paramerters for Mega configs.

  Attributes:
    pad_token_id: the token id for the pad token
    low_rank_features: number of dimensions for low-rank projection
  """

  zdim: int = 64
  hdim: int = 256
  ndim: int = 16
  activation: str = 'silu'
  bidirectional: bool = False
  dropout: float = 0.0
  hidden_dropout: float = 0.0


@base_config.bind(MegaEncoderConfig)
def get_encoder(encoder_cfg: MegaEncoderConfig):
  """Gets a 'MegaEncoder' object.

  Args:
    encoder_cfg: A 'MegaEncoderConfig'.

  Returns:
    A encoder object.
  """
  encoder = MegaEncoder(
      vocab_size=encoder_cfg.vocab_size,
      hidden_size=encoder_cfg.hidden_size,
      num_layers=encoder_cfg.num_layers,
      zdim=encoder_cfg.zdim,
      hdim=encoder_cfg.hdim,
      ndim=encoder_cfg.ndim,
      activation=encoder_cfg.activation,
      bidirectional=encoder_cfg.bidirectional,
      dropout=encoder_cfg.dropout,
      hidden_dropout=encoder_cfg.hidden_dropout,
      inner_activation=tf_utils.get_activation(encoder_cfg.hidden_activation),
      attention_dropout=encoder_cfg.attention_dropout_rate,
      max_sequence_length=encoder_cfg.max_position_embeddings,
      type_vocab_size=encoder_cfg.type_vocab_size,
      initializer=tf_keras.initializers.TruncatedNormal(
          stddev=encoder_cfg.initializer_range
      ),
      output_range=encoder_cfg.output_range,
      embedding_width=encoder_cfg.embedding_size,
      norm_first=encoder_cfg.norm_first,
  )
  return encoder
