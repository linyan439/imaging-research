#!/usr/bin/python
#
# Copyright 2023 Google LLC
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
"""Collection of functions to generate embeddings."""
import base64
import enum
import io
import logging
import os
from typing import Any, Iterable, Sequence, Union

from cxr_foundation import constants
from cxr_foundation import example_generator_lib
from google.api_core import exceptions
from google.api_core.client_options import ClientOptions
from google.api_core.retry import Retry
from google.cloud import aiplatform
import numpy as np
from PIL import Image
import pydicom
import tensorflow as tf


_RETRIABLE_TYPES = (
    exceptions.TooManyRequests,  # HTTP 429
    exceptions.InternalServerError,  # HTTP 500
    exceptions.BadGateway,  # HTTP 502
    exceptions.ServiceUnavailable,  # HTTP 503
    exceptions.DeadlineExceeded,  # HTTP 504
)

_API_ENDPOINT = 'us-central1-aiplatform.googleapis.com'
_VIEW_POSITION = 'ViewPosition'
_FRONTAL_VIEW_POSITIONS = ('AP', 'PA')

_ELIXR_B_RESPONSE_KEY = 'img_emb'
_ELIXR_B_RESPONSE_SHAPE = (32, 768)
_ELIXR_C_RESPONSE_SHAPE = (1, 8, 8, 1376)


class ModelVersion(enum.Enum):
  V1 = enum.auto()  # CXR Foundation model V1.
  V2 = enum.auto()  # 2-stage ELIXR model.


class InputFileType(enum.Enum):
  PNG = 'png'
  DICOM = 'dicom'

  def __str__(self):
    return self.value


class OutputFileType(enum.Enum):
  TFRECORD = 'tfrecord'
  NPZ = 'npz'

  def __str__(self):
    return self.value


def _image_id_to_filebase(image_id: str) -> str:
  filebase, _ = os.path.splitext(os.path.basename(image_id))
  return filebase


def _output_file_name(
    input_file: str, output_dir: str, format: OutputFileType
) -> str:
  filebase = _image_id_to_filebase(input_file)
  if format == OutputFileType.TFRECORD:
    return os.path.join(output_dir, f'{filebase}.tfrecord')
  elif format == OutputFileType.NPZ:
    return os.path.join(output_dir, f'{filebase}.npz')
  raise ValueError('Unknown file type.')


def generate_embeddings(
    input_files: Iterable[str],
    output_dir: str,
    input_type: InputFileType,
    output_type: OutputFileType,
    overwrite_existing: bool = False,
    model_version: ModelVersion = ModelVersion.V1,
) -> None:
  """Generate embedding files from a set of input image files.

  Parameters
  ----------
  input_files
    The set of image files to generate the embeddings from.
  output_dir
    The directory to write the embedding files to. The output file names will be
    constructed
    from the base name of the input files and the output file type.
  input_type
    The file type of the input images. DICOM or PNG.
  overwrite_existing
    If an output file already exists, whether to overwrite or skip inference.
  model_version
    The CXR foundation model version.

  Raises
  ------
    ValueError
      If the `model_version` is unsupported.
  """
  if model_version == ModelVersion.V1:
    embeddings_fn = embeddings_v1
  elif model_version == ModelVersion.V2:
    embeddings_fn = embeddings_v2
  else:
    raise ValueError('Model version {model_version.name!r} is unsupported.')

  for file in input_files:
    output_file = _output_file_name(
        file, output_dir=output_dir, format=output_type
    )

    if not overwrite_existing and os.path.exists(output_file):
      logging.info(f'Found existing output file. Skipping: {output_file!r}')
      continue

    image_example = create_example_from_image(
        image_file=file, input_type=input_type
    )
    assert constants.IMAGE_KEY in image_example.features.feature

    embeddings = embeddings_fn(image_example)

    save_embeddings(
        embeddings,
        output_file=output_file,
        format=output_type,
        image_example=image_example,
    )
    logging.info(f'Successfully generated {output_file!r}')


def embeddings_v1(image_example: tf.train.Example) -> np.ndarray:
  """Create CXR Foundation V1 model embeddings.

  Parameters
  ----------
  image_example: TF Example with image bytes.

  Returns
  -------
  NumPy array of shape (1376,).
  """
  response = _embeddings_from_service(
      image_example,
      constants.ENDPOINT_V1.project_name,
      constants.ENDPOINT_V1.endpoint_location,
      constants.ENDPOINT_V1.endpoint_id,
  )
  assert len(response) == 1
  assert len(response[0]) == 1
  embeddings = np.array(response[0][0], dtype=np.float32)
  assert embeddings.shape == (1376,)
  return embeddings


def embeddings_v2(image_example: tf.train.Example) -> np.ndarray:
  """Create CXR Foundation V2 model embeddings.

  This is a two-step process:
  - Query ELIXR-C for a 1x8x8x1376 dimension embedding.
  - Query ELIXR-B with the embedding from the previous step to obtain a semantic
    embedding for the text generation model.

  Parameters
  ----------
  image_example: TF Example with image bytes.

  Returns
  -------
  NumPy array of shape (32, 768).
  """
  elixr_c_response = _embeddings_from_service(
      image_example,
      constants.ENDPOINT_V2_C.project_name,
      constants.ENDPOINT_V2_C.endpoint_location,
      constants.ENDPOINT_V2_C.endpoint_id,
  )
  elixr_c_embedding = np.expand_dims(
      np.array(elixr_c_response[0], dtype=np.float32), axis=0
  )
  assert elixr_c_embedding.shape == _ELIXR_C_RESPONSE_SHAPE
  elixr_b_response = _embeddings_from_service(
      elixr_c_embedding,
      constants.ENDPOINT_V2_B.project_name,
      constants.ENDPOINT_V2_B.endpoint_location,
      constants.ENDPOINT_V2_B.endpoint_id,
  )
  assert len(elixr_b_response) == 1
  assert _ELIXR_B_RESPONSE_KEY in elixr_b_response[0]
  elixr_b_embedding = np.array(
      elixr_b_response[0][_ELIXR_B_RESPONSE_KEY], dtype=np.float32
  )
  assert elixr_b_embedding.shape == _ELIXR_B_RESPONSE_SHAPE
  return elixr_b_embedding


def create_example_from_image(
    image_file: str, input_type: InputFileType
) -> tf.train.Example:
  """Create a tf.train.Example from an image file."""
  with open(image_file, 'rb') as f:
    if input_type == InputFileType.PNG:
      img = np.asarray(Image.open(io.BytesIO(f.read())).convert('L'))
      return example_generator_lib.png_to_tfexample(img)
    elif input_type == InputFileType.DICOM:
      dicom = pydicom.dcmread(io.BytesIO(f.read()))
      if (
          _VIEW_POSITION in dicom
          and dicom.ViewPosition not in _FRONTAL_VIEW_POSITIONS
      ):
        raise RuntimeError(
            f'DICOM file: {image_file} - view position is not in accepted'
            ' set: ',
            _FRONTAL_VIEW_POSITIONS,
        )
      return example_generator_lib.dicom_to_tfexample(dicom)

    raise ValueError('Unknown file type.')


def _is_retryable(exc):
  return isinstance(exc, _RETRIABLE_TYPES)


def _embeddings_from_service(
    example_or_array: Union[np.ndarray, tf.train.Example],
    project_name: str,
    location: str,
    endpoint_id: int,
) -> Any:
  """Returns embeddings from a Vertex (AI Platform) model prediction endpoint.

  Parameters
  ----------
  example_or_array
    The input is encoded as a JSON-like Python object before transmission.
    If this is a tf.train.Example, it should contain the original image bytes.
    The expected object schema is defined by `create_example_from_image`. The
    Example proto is encoded as:
    ```
    [
      'b64': <base64-encoded serialized `image_example`>
    ]
    ```
    A NumPy array is converted to a list and encoded as:
    ```
    [
      'image_feature': array.tolist()
    ]
    ```
  project_name
    The GCP project name that hosts embeddings API.
  location
    The GCP Location (Zone) where the model serving end-point is deployed.
  endpoint_id
    The numerical endpoint ID of the embeddings API.

  Returns
  ------
  The image embeddings generated by the service. Differences in Vertex
  end-point configurations may change the return type. The caller is
  responsible for interpreting this value and extracting the requisite
  data.

  Raises
  ------
    TypeError
      If `example_or_array` is neither an Example nor a NumPy array.
  """
  if type(example_or_array) == tf.train.Example:
    instances = [
        {'b64': base64.b64encode(example_or_array.SerializeToString()).decode()}
    ]
  elif type(example_or_array) == np.ndarray:
    instances = [{'image_feature': example_or_array.tolist()}]
  else:
    raise TypeError(
        f'Unsupported `example_or_array` type: {type(example_or_array)}'
    )

  api_client = aiplatform.gapic.PredictionServiceClient(
      client_options=ClientOptions(api_endpoint=_API_ENDPOINT)
  )

  endpoint = api_client.endpoint_path(
      project=project_name, location=location, endpoint=endpoint_id
  )
  retry_policy = Retry(predicate=_is_retryable)
  response = api_client.predict(
      endpoint=endpoint, instances=instances, retry=retry_policy, timeout=60
  )
  return response.predictions


def save_embeddings(
    embeddings: np.ndarray,
    output_file: str,
    format: OutputFileType,
    image_example: tf.train.Example = None,
):
  """Save the embeddings values to a numpy or tfrecord file.

  Parameters
  ---------
  embeddings
    The vector embeddings values to save
  output_file
    The file path to save to
  format
    The format to save the embeddings to - .npz or .tfrecord.
  image_example
    The original Example generated from the image. This is only required if
    saving as .tfrecord.
  """
  embeddings_array = embeddings.astype(np.float32).flatten()

  if format == OutputFileType.NPZ:
    # Keyed by "embedding"
    np.savez(output_file, embedding=embeddings_array)
  elif format == OutputFileType.TFRECORD:
    if image_example is None:
      raise RuntimeError(
          'Missing image_example param required for saving as tfrecord.'
      )

    # Add embeddings values to example
    image_example.features.feature[constants.EMBEDDING_KEY].float_list.value[
        :
    ] = embeddings_array

    # Remove unnecessary existing fields to prevent serializing them
    for key in (constants.IMAGE_FORMAT_KEY, constants.IMAGE_KEY):
      if key in image_example.features.feature:
        del image_example.features.feature[key]

    with tf.io.TFRecordWriter(output_file) as w:
      w.write(image_example.SerializeToString())

  else:
    raise ValueError('Unknown file type.')
