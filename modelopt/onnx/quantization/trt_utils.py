# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module contains TensorRT utils."""

import ctypes
import logging
import platform
from typing import Dict, List, Optional, Tuple

import numpy as np
import onnx
import onnx_graphsurgeon as gs

from modelopt.onnx.utils import get_dynamic_graph_inputs, parse_shapes_spec

try:
    import tensorrt as trt

    TRT_PYTHON_AVAILABLE = True
except ImportError:
    TRT_PYTHON_AVAILABLE = False


def get_custom_layers(onnx_path: str, trt_plugins: Optional[str]) -> Tuple[List[str], Dict]:
    """Gets custom layers in ONNX model.

    Args:
        onnx_path: Path to the input ONNX model.
        trt_plugins: Paths to custom TensorRT plugins.

    Returns:
        List of custom layers.
        Dictionary containing tensors information: {'tensor_name': {'shape': tensor.shape, 'dtype': tensor.dtype}}
    """
    # Initialize TensorRT plugins
    if trt_plugins is not None:
        trt_plugins = trt_plugins.split(";")
        for plugin in trt_plugins:
            ctypes.CDLL(plugin)

    # Create builder and network
    trt_logger = trt.Logger(trt.Logger.WARNING)
    trt.init_libnvinfer_plugins(trt_logger, "")
    builder = trt.Builder(trt_logger)
    network = builder.create_network()

    # Parse ONNX file
    parser = trt.OnnxParser(network, trt_logger)
    if not parser.parse_from_file(onnx_path):
        error_str = [str(parser.get_error(error)) for error in range(parser.num_errors)]
        raise Exception(f"Failed to parse ONNX file: {''.join(error_str)}")

    # Obtain layer info
    custom_layers = []
    all_tensor_info = {}
    for layer_idx in range(network.num_layers):
        layer = network.get_layer(layer_idx)

        # Obtain plugin layer names
        if "PLUGIN" in str(layer.type):
            custom_layers.append(layer.name)

        # Collect all tensors' type and shape.
        # Replace dynamic axis representation from -1 to 'unk' for 'onnxsim' support if enabled.
        for i in range(layer.num_inputs):
            input_tensor = layer.get_input(i)
            if input_tensor:
                all_tensor_info[input_tensor.name] = {
                    "shape": ["unk" if (s == -1) else s for s in input_tensor.shape],
                    "dtype": input_tensor.dtype,
                }

        for i in range(layer.num_outputs):
            output_tensor = layer.get_output(i)
            if output_tensor and not all_tensor_info.get(output_tensor.name, None):
                all_tensor_info[output_tensor.name] = {
                    "shape": ["unk" if (s == -1) else s for s in output_tensor.shape],
                    "dtype": output_tensor.dtype,
                }

    return custom_layers, all_tensor_info


def infer_types_shapes(graph: gs.Graph, all_tensor_info: Dict) -> None:
    """Updates tensor shapes in ORT graph.

    Args:
        graph: ONNX model's GS graph.
        all_tensor_info: Dictionary containing tensors information.

    Returns:
        None. In-memory modification of graph.
    """

    def _map_trt_to_python_type(trt_type: trt.DataType):
        try:
            return trt.nptype(trt_type)
        except TypeError as e:
            logging.warning(f"{e}. TRT datatype: {trt_type}. Setting to None.")
            return None

    for node in graph.nodes:
        for out in node.outputs:
            if out.name in all_tensor_info:
                out.shape = all_tensor_info[out.name]["shape"]
                out.dtype = out.dtype or _map_trt_to_python_type(all_tensor_info[out.name]["dtype"])
    graph.cleanup().toposort()


def load_onnx_model(
    onnx_path: str,
    trt_plugins: Optional[str] = None,
    calibrations_shapes: str = None,
    use_external_data_format: bool = False,
    intermediate_generated_files: list[str] = None,
) -> tuple[onnx.onnx_pb.ModelProto, bool, list[str], str]:
    """Load ONNX model. If 'tensorrt' is installed, check if the model has custom ops and ensure it's supported by ORT.

    Args:
        onnx_path: Path to the input ONNX model.
        trt_plugins: Paths to custom TensorRT plugins.
        calibrations_shapes: Static shapes for graph inputs.
        use_external_data_format: If True, separate data path will be used to store the weights of the quantized model.
        intermediate_generated_files: List of paths of intermediate ONNX files, generated during quantization.

    Returns:
        Loaded ONNX model supported by ORT.
        Boolean indicating whether the model has custom ops or not.
        List of custom ops in the ONNX model.
        Path to new intermediary ONNX model.
    """
    custom_ops = []
    has_custom_op = False

    # Load the model and weights
    onnx_model = onnx.load(onnx_path, load_external_data=use_external_data_format)

    # If inputs are dynamic and calibration shapes are given, set them as static
    dynamic_inputs = get_dynamic_graph_inputs(onnx_model)
    onnx_path_static_shapes = None
    if len(dynamic_inputs) > 0:
        input_names = [inp.name for inp in dynamic_inputs]
        logging.info(f"Model has dynamic inputs: {input_names}!")

        if calibrations_shapes:
            calibration_shapes_arr = parse_shapes_spec(calibrations_shapes)
            for graph_input in onnx_model.graph.input:
                if graph_input.name not in input_names:
                    continue
                inp_shapes = calibration_shapes_arr[graph_input.name]
                logging.info(f" Setting '{graph_input.name}' shape to {inp_shapes}!")
                for idx, (d, s) in enumerate(
                    zip(graph_input.type.tensor_type.shape.dim, inp_shapes)
                ):
                    if not d.dim_value:
                        graph_input.type.tensor_type.shape.dim[idx].dim_value = s

            onnx_path_static_shapes = onnx_path.replace(".onnx", "_static.onnx")
            onnx.save(
                onnx_model, onnx_path_static_shapes, save_as_external_data=use_external_data_format
            )
            intermediate_generated_files.append(onnx_path_static_shapes)
            logging.info(f"Model with static shapes is saved to {onnx_path_static_shapes}!")

    if TRT_PYTHON_AVAILABLE and platform.system() != "Windows":
        # Check if there's a custom TensorRT op in the ONNX model. If so, make it ORT compatible by adding
        #   `trt.plugins to the ONNX graph.
        trt_plugin_domain = "trt.plugins"
        trt_plugin_version = 1

        custom_layers, all_tensor_info = get_custom_layers(
            onnx_path_static_shapes or onnx_path, trt_plugins
        )
        has_custom_op = True if custom_layers else False

        if has_custom_op:
            graph = gs.import_onnx(onnx_model)
            for node in graph.nodes:
                if node.name in custom_layers:
                    custom_ops.append(node.op)
                    node.domain = trt_plugin_domain
            custom_ops = np.unique(custom_ops)

            # Ensure that all tensors in the graph have type and shape info
            infer_types_shapes(graph, all_tensor_info)

            # Add TRT domain and version to the graph
            onnx_model = gs.export_onnx(graph)
            onnx_model.opset_import.append(
                onnx.helper.make_opsetid(trt_plugin_domain, trt_plugin_version)
            )

    return onnx_model, has_custom_op, custom_ops, onnx_path_static_shapes or onnx_path
