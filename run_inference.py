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
"""A Beam pipeline to generate embeddings from a list of PNG or DICOM files."""
import constants
import inference_beam_lib

import argparse
import glob
import logging
import os

import apache_beam as beam
from apache_beam.options import pipeline_options

_REQUIREMENTS_FILE = 'requirements.txt'
_SETUP_FILE = './setup.py'


def main(argv=None, save_main_session=True):
  parser = argparse.ArgumentParser()
  parser.add_argument(
      '--input_path',
      dest='input_path',
      required=True,
      help='Folder or GCS bucket containing input PNG or DICOM files.')
  parser.add_argument(
      '--output_path',
      dest='output_path',
      required=True,
      help='Folder or GCS bucket to write output tf.Examples.')
  parser.add_argument(
      '--limit',
      dest='limit',
      default=100,
      type=int,
      help='Only process this many input files.')
  parser.add_argument(
      '--endpoint_id',
      dest='endpoint_id',
      required=True,
      type=int,
      help='Numerical ID for embeddings API.')
  parser.add_argument(
      '--embeddings_project',
      dest='embeddings_project',
      required=True,
      help='GCP project ID that hosts embeddings API.')
  known_args, pipeline_args = parser.parse_known_args(argv)
  options = pipeline_options.PipelineOptions(pipeline_args)

  if known_args.input_path.startswith(constants.GCS_PREFIX):
    input = list(beam.io.gcp.gcsio.GcsIO().list_prefix(known_args.input_path))
  else:
    input = [
        os.path.join(known_args.input_path, f)
        for f in os.listdir(known_args.input_path)
    ]

  if known_args.limit > 0:
    input = input[:known_args.limit]

  options.view_as(
      pipeline_options.SetupOptions).save_main_session = save_main_session
  options.view_as(
      pipeline_options.SetupOptions).requirements_file = _REQUIREMENTS_FILE
  options.view_as(pipeline_options.SetupOptions).setup_file = _SETUP_FILE

  with beam.Pipeline(options=options) as p:
    _ = (
        p | beam.Create(input) | beam.ParDo(
            inference_beam_lib.CreateExampleDoFn(
                output_path=known_args.output_path))
        | beam.ParDo(
            inference_beam_lib.GenerateEmbeddingsDoFn(
                known_args.embeddings_project, known_args.endpoint_id))
        | beam.ParDo(
            inference_beam_lib.ProcessPredictionDoFn(
                output_path=known_args.output_path))
        | beam.Map(print))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  main()
