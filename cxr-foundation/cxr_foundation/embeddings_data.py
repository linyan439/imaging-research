#!/usr/bin/python
#
# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Module for reading and creating datasets from embeddings files generated by the CXR Foundation service.

Unless specified otherwise, these functions are NOT generalizable/usable on embeddings files
or tfrecord files from other sources.


Expected structure of the generated TFRecord proto:

features {
  feature {
    key: "embedding"
    value {
      float_list {
        value: -1.3924500942230225
        value: 0.4983276426792145
        value: 1.1395248174667358
        value: 0.6487054228782654
        ...
      }
    }
  }
}
"""
from typing import Iterable

import numpy as np
import tensorflow as tf

from cxr_foundation import constants


def read_tfrecord_example(filename: str) -> tf.train.Example:
  """
  Read the tf.Example data contained in a single TFRecord embedding file.

  Args:
    filename: The path of the .tfrecord file to read

  Returns:
    The `tf.Example` data contained in the TFRecord.

  Note: This is a convenience function for exploring/exporting. Do not use this in TF pipelines.

  """
  raw_dataset = tf.data.TFRecordDataset(filename)
  # Expect only one element in the TFRecord.
  for raw_record in raw_dataset.take(1):
    example = tf.train.Example()
    example.ParseFromString(raw_record.numpy())

  return example

def _parse_example_values(example_data: tf.train.Example) -> np.ndarray:
  """
  Extract the embeddings values contained in an Example object, extracted from a file
  generated by the CXR foundation service. Helper function for `read_record_values`.

  Args:
    example_data: The Example object to extract the values from

  Returns:
    The 1D float array of the embeddings values

  Note: This is a convenience function for exploring/exporting. Do not use this in TF pipelines.

  """
  # Unpack nested proto and create np array from google.protobuf.pyext._message.RepeatedScalarContainer
  try:
    values = np.array(example_data.features.feature[constants.EMBEDDING_KEY].float_list.value, dtype='float32')
    return values
  except ValueError:
    print(f"Input Example does not contain expected CXR Foundation embedding structure.")
    raise

def read_tfrecord_values(filename: str) -> np.ndarray:
  """
  Read the embeddings values contained in a .tfrecord embedding file, generated by this library.

  Args:
    filename: The path of the .tfrecord file to read

  Returns:
    The 1D float array of the embeddings values

  Note: This is a convenience function for exploring/exporting. Do not use this in TF pipelines.

  """
  return _parse_example_values(read_tfrecord_example(filename))


def read_npz_values(filename: str) -> np.ndarray:
  """
  Read the embeddings values contained in a .npz embedding file, generated by this package.

  Args:
    filename: The path of the .npz file to read

  Returns:
    The 1D float array of the embeddings values

  Note: You can load the generated .npz files without installing this package and its dependencies, by copying this simple function. You would only need to install Numpy.

  """
  data = np.load(filename)
  return data[constants.EMBEDDING_KEY]


def parse_serialized_example_values(
    serialized_example: bytes,
) -> tf.Tensor:
  """
  Parses and extracts the embeddings values from a serialized tf.Example generated by the CXR foundation service.

  Args:
    serialized_example: The bytes of the tf.Example to parse.

  Returns:
    The 1D Tensor of float embeddings

  """
  features = {
      constants.EMBEDDING_KEY:
          tf.io.FixedLenFeature([constants.DEFAULT_EMBEDDINGS_SIZE],
                                tf.float32,
                                default_value=tf.constant(
                                    0.0, shape=[constants.DEFAULT_EMBEDDINGS_SIZE]))
  }
  parsed_tensors = tf.io.parse_example(serialized_example, features=features)
  return parsed_tensors[constants.EMBEDDING_KEY]


def get_dataset(
    filenames: Iterable[str],
    labels: Iterable[int]
) -> tf.data.Dataset:
  """
  Create a tf.data.Dataset from the specified tfrecord files and labels.

  Args:
    filenames: The set of .tfrecord file names.
    labels: The corresponding label for each record.

  Returns:
    The Dataset, containing for each element: (embeddings, label)
  """
  ds_embeddings = tf.data.TFRecordDataset(filenames, num_parallel_reads=tf.data.AUTOTUNE).map(
        parse_serialized_example_values
   )
  ds_labels = tf.data.Dataset.from_tensor_slices(labels)

  return tf.data.Dataset.zip((ds_embeddings, ds_labels))
