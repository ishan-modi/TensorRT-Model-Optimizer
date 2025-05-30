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

"""Tests of tensor quantization function and module common to cpu and cuda"""

import pytest
import torch
from _test_utils.torch_quantization.quant_utils import quant
from torch.nn.parameter import Parameter

from modelopt.torch.quantization import tensor_quant


class TensorQuantCommon:
    device = None
    func = None
    is_fake = None
    return_tuple = None

    def test_simple_run(self):
        """quantizer passes gradcheck"""
        x = Parameter(torch.randn(2, 3, dtype=torch.float64).to(self.device)) * 100
        self.func(x, torch.max(torch.abs(x)), None, 7)

    def test_per_tensor_scale(self):
        """Tensor_quant matches quantization"""
        x = torch.randn(31).to(self.device)
        quant_x_ref = quant(
            x,
            torch.max(x.abs()),
            fake=self.is_fake,
        )
        quant_x_test = self.func(x, torch.max(torch.abs(x)))
        if self.return_tuple:
            quant_x_test = quant_x_test[0]
        assert torch.allclose(quant_x_ref, quant_x_test)

    def test_per_channel_scale(self):
        """fake_tensor_quant performs per channel quantization"""
        torch.manual_seed(123)
        x = torch.randn(3, 3, 6, 8).to(self.device)

        # Pytorch filter layout seems to be KCRS, reduce max to shape [K, 1, 1, 1] to test per channel scale
        # Shrink max a little, so that clip behavior is tested
        amax_x = 0.7 * torch.amax(x.abs(), dim=(1, 2, 3), keepdims=True)
        quant_x_ref = quant(
            x,
            amax_x,
            fake=self.is_fake,
        )
        quant_x_test = self.func(x, amax_x)
        if self.return_tuple:
            quant_x_test = quant_x_test[0]

        assert torch.allclose(quant_x_test, quant_x_ref)

    def test_backward(self):
        """Tensor_quant implements straight through estimator on the backward pass
        Note: this does not work for integer output_dtype
        """
        x = torch.randn(3, 7, requires_grad=True).to(self.device)
        labels = torch.randint(6, (3,)).type(torch.LongTensor).to(self.device)
        quant_x = self.func(x, x.abs().max(), None, 7)
        if self.return_tuple:
            quant_x = quant_x[0]
        float_quant_x = quant_x.type(torch.FloatTensor).to(self.device)
        x.retain_grad()
        float_quant_x.retain_grad()
        criterion = torch.nn.CrossEntropyLoss().to(self.device)
        loss = criterion(float_quant_x, labels)
        loss.backward()
        assert torch.allclose(float_quant_x.grad, x.grad)

    def test_unsigned(self):
        x = torch.randn(31).abs().to(self.device)
        quant_x_ref = quant(
            x,
            torch.max(x.abs()),
            num_bits=9,
            fake=self.is_fake,
        )
        quant_x_test = self.func(x, torch.max(torch.abs(x)), None, 8, True)
        if self.return_tuple:
            quant_x_test = quant_x_test[0]
        assert torch.allclose(quant_x_test, quant_x_ref)

        x = torch.randn(3, 7)
        with pytest.raises(TypeError, match="Negative values encountered"):
            self.func(x, torch.max(torch.abs(x)), None, 8, True)

    def test_clip_gradient(self):
        x = torch.randn(3, 7, requires_grad=True).to(self.device)
        x.retain_grad()
        amax = x.abs().max() / 2
        x_in_range = (-amax <= x) * (x <= amax)
        quant_x = self.func(x, amax, None, 8)
        if self.return_tuple:
            quant_x = quant_x[0]
        loss = torch.sum((quant_x - 0.5) ** 2)
        loss.backward()
        assert torch.allclose(x.grad != 0, x_in_range)

    def test_full_range(self):
        """fake_tensor_quant uses the full integer range when narrow=False"""
        x = torch.randn(31).abs().to(self.device)
        amax = torch.max(x.abs())
        quant_x_ref = quant(x, amax, num_bits=9, fake=self.is_fake, narrow_range=False)
        quant_x_test = self.func(x, torch.max(torch.abs(x)), None, 8, True, False)
        if self.return_tuple:
            quant_x_test = quant_x_test[0]
        assert torch.allclose(quant_x_test, quant_x_ref)


class TensorQuantTester(TensorQuantCommon):
    func = tensor_quant.tensor_quant
    is_fake = False
    return_tuple = True

    def test_overflow_fp16(self):
        x = torch.randn(31).to(self.device).half()
        with pytest.raises(ValueError, match="scale is too large for FP16"):
            _ = self.func(x, torch.tensor(1e-4).to(self.device).half(), None, 8, False)


class FakeTensorQuantTester(TensorQuantCommon):
    func = tensor_quant.fake_tensor_quant
    is_fake = True
    return_tuple = False

    def test_overflow_fp16(self):
        x = torch.randn(31).to(self.device).half()
        quant_x_test = tensor_quant.fake_tensor_quant(
            x, torch.tensor(1e-4).to(self.device).half(), 8, False
        )
        assert not (torch.isinf(quant_x_test).any() or torch.isnan(quant_x_test).any())

    @pytest.mark.parametrize("dtype", [torch.float32, torch.float16])
    @pytest.mark.parametrize("num_bits", [3, 4, 5, 7, 8, 11])
    @pytest.mark.parametrize("unsigned", [True, False])
    def test_against_legacy(self, dtype, num_bits, unsigned):
        torch.manual_seed(123456)
        x = torch.randn(3, 4, 5, 6).to(dtype).to(self.device)

        amax_torch = torch.tensor(0.7).to(self.device)

        if unsigned:
            x = x.abs()
        legacy_out = tensor_quant.legacy_fake_tensor_quant(x, amax_torch, None, num_bits, unsigned)
        test_out = tensor_quant.fake_tensor_quant(x, amax_torch, None, num_bits, unsigned)
        if dtype == torch.float16:
            assert torch.allclose(legacy_out, test_out, rtol=1e-3, atol=1e-4)
        else:
            assert torch.allclose(legacy_out, test_out)

    def test_against_legacy_noncontiguous(self):
        x = torch.randn(3, 4, 5, 6).to(self.device)

        amax_torch = torch.tensor(0.7).to(self.device)

        x_torch_noncontiguous = x[:, 2, :, 3]
        assert not x_torch_noncontiguous.is_contiguous()

        legacy_out = tensor_quant.legacy_fake_tensor_quant(x_torch_noncontiguous, amax_torch, None)
        test_out = tensor_quant.fake_tensor_quant(x_torch_noncontiguous, amax_torch, None)
        assert torch.allclose(legacy_out, test_out, rtol=0, atol=0)

    @pytest.mark.parametrize("dtype", [torch.float32, torch.float16])
    @pytest.mark.parametrize("num_bits", [3, 4, 5, 7, 8, 11])
    @pytest.mark.parametrize("unsigned", [True, False])
    def test_against_legacy_with_axis(self, dtype, num_bits, unsigned):
        x = torch.randn(3, 4, 5, 6).to(self.device).to(dtype)

        # amax along axis 1
        amax_torch = torch.tensor([0.8, 0.9, 0.7, 0.6]).to(self.device).view(1, -1, 1, 1)

        if unsigned:
            x = x.abs()
        legacy_out = tensor_quant.legacy_fake_tensor_quant(x, amax_torch, None, num_bits, unsigned)
        test_out = tensor_quant.fake_tensor_quant(x, amax_torch, None, num_bits, unsigned)
        assert torch.allclose(
            legacy_out, test_out, atol=1e-3 if dtype == torch.float16 else 0, rtol=0
        )


class FakeAffineTensorQuantTester:
    device = None

    def test_simple_run(self):
        x = torch.tensor([-1.0, -13.0, -101.0, -128.0, 0.0, 2.0, 5.0, 13.0, 93.0, 111.0, 127.0]).to(
            self.device
        )
        quant_x = tensor_quant.fake_affine_tensor_quant(x, torch.min(x), torch.max(x))
        assert torch.allclose(quant_x, x)

    def test_clip_gradient(self):
        x = torch.randn(3, 7, requires_grad=True).to(self.device)
        x.retain_grad()
        xmin = x.min() / 2
        xmax = x.max() / 2
        x_in_range = (xmin <= x) * (x <= xmax)
        quant_x = tensor_quant.fake_affine_tensor_quant(x, xmin, xmax, 8)
        loss = torch.sum((quant_x - 0.5) ** 2)
        loss.backward()
        assert torch.equal(x.grad != 0, x_in_range)
