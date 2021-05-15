#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from itertools import product

import aesara
import aesara.tensor as at
import numpy as np
import numpy.ma as ma
import numpy.testing as npt
import pandas as pd
import pytest
import scipy.sparse as sps

from aesara.graph.basic import Constant, Variable, ancestors
from aesara.tensor.random.basic import normal, uniform
from aesara.tensor.random.op import RandomVariable
from aesara.tensor.subtensor import AdvancedIncSubtensor, AdvancedIncSubtensor1
from aesara.tensor.type import TensorType
from aesara.tensor.var import TensorVariable

import pymc3 as pm

from pymc3.aesaraf import (
    _conversion_map,
    change_rv_size,
    extract_obs_data,
    pandas_to_array,
    rvs_to_value_vars,
    take_along_axis,
    walk_model,
)
from pymc3.exceptions import ShapeError
from pymc3.vartypes import int_types

FLOATX = str(aesara.config.floatX)
INTX = str(_conversion_map[FLOATX])


def test_change_rv_size():
    loc = at.as_tensor_variable([1, 2])
    rv = normal(loc=loc)
    assert rv.ndim == 1
    assert rv.eval().shape == (2,)

    with pytest.raises(ShapeError, match="must be ≤1-dimensional"):
        change_rv_size(rv, new_size=[[2, 3]])
    with pytest.raises(ShapeError, match="must be ≤1-dimensional"):
        change_rv_size(rv, new_size=at.as_tensor_variable([[2, 3], [4, 5]]))

    rv_new = change_rv_size(rv, new_size=(3,), expand=True)
    assert rv_new.ndim == 2
    assert rv_new.eval().shape == (3, 2)

    # Make sure that the shape used to determine the expanded size doesn't
    # depend on the old `RandomVariable`.
    rv_new_ancestors = set(ancestors((rv_new,)))
    assert loc in rv_new_ancestors
    assert rv not in rv_new_ancestors

    rv_newer = change_rv_size(rv_new, new_size=(4,), expand=True)
    assert rv_newer.ndim == 3
    assert rv_newer.eval().shape == (4, 3, 2)

    # Make sure we avoid introducing a `Cast` by converting the new size before
    # constructing the new `RandomVariable`
    rv = normal(0, 1)
    new_size = np.array([4, 3], dtype="int32")
    rv_newer = change_rv_size(rv, new_size=new_size, expand=False)
    assert rv_newer.ndim == 2
    assert isinstance(rv_newer.owner.inputs[1], Constant)
    assert rv_newer.eval().shape == (4, 3)


class TestBroadcasting:
    def test_make_shared_replacements(self):
        """Check if pm.make_shared_replacements preserves broadcasting."""

        with pm.Model() as test_model:
            test1 = pm.Normal("test1", mu=0.0, sigma=1.0, size=(1, 10))
            test2 = pm.Normal("test2", mu=0.0, sigma=1.0, size=(10, 1))

        # Replace test1 with a shared variable, keep test 2 the same
        replacement = pm.make_shared_replacements(
            test_model.initial_point, [test_model.test2], test_model
        )
        assert (
            test_model.test1.broadcastable
            == replacement[test_model.test1.tag.value_var].broadcastable
        )

    def test_metropolis_sampling(self):
        """Check if the Metropolis sampler can handle broadcasting."""
        with pm.Model() as test_model:
            test1 = pm.Normal("test1", mu=0.0, sigma=1.0, size=(1, 10))
            test2 = pm.Normal("test2", mu=test1, sigma=1.0, size=(10, 10))

            step = pm.Metropolis()
            # TODO FIXME: Assert whatever it is we're testing
            pm.sample(tune=5, draws=7, cores=1, step=step, compute_convergence_checks=False)


def _make_along_axis_idx(arr_shape, indices, axis):
    # compute dimensions to iterate over
    if str(indices.dtype) not in int_types:
        raise IndexError("`indices` must be an integer array")
    shape_ones = (1,) * indices.ndim
    dest_dims = list(range(axis)) + [None] + list(range(axis + 1, indices.ndim))

    # build a fancy index, consisting of orthogonal aranges, with the
    # requested index inserted at the right location
    fancy_index = []
    for dim, n in zip(dest_dims, arr_shape):
        if dim is None:
            fancy_index.append(indices)
        else:
            ind_shape = shape_ones[:dim] + (-1,) + shape_ones[dim + 1 :]
            fancy_index.append(np.arange(n).reshape(ind_shape))

    return tuple(fancy_index)


if hasattr(np, "take_along_axis"):
    np_take_along_axis = np.take_along_axis
else:

    def np_take_along_axis(arr, indices, axis):
        if arr.shape[axis] <= 32:
            # We can safely test with numpy's choose
            arr = np.moveaxis(arr, axis, 0)
            indices = np.moveaxis(indices, axis, 0)
            out = np.choose(indices, arr)
            return np.moveaxis(out, 0, axis)
        else:
            # numpy's choose cannot handle such a large axis so we
            # just use the implementation of take_along_axis. This is kind of
            # cheating because our implementation is the same as the one below
            if axis < 0:
                _axis = arr.ndim + axis
            else:
                _axis = axis
            if _axis < 0 or _axis >= arr.ndim:
                raise ValueError(f"Supplied axis {axis} is out of bounds")
            return arr[_make_along_axis_idx(arr.shape, indices, _axis)]


class TestTakeAlongAxis:
    def setup_class(self):
        self.inputs_buffer = dict()
        self.output_buffer = dict()
        self.func_buffer = dict()

    def _input_tensors(self, shape):
        ndim = len(shape)
        arr = TensorType(FLOATX, [False] * ndim)("arr")
        indices = TensorType(INTX, [False] * ndim)("indices")
        arr.tag.test_value = np.zeros(shape, dtype=FLOATX)
        indices.tag.test_value = np.zeros(shape, dtype=INTX)
        return arr, indices

    def get_input_tensors(self, shape):
        ndim = len(shape)
        try:
            return self.inputs_buffer[ndim]
        except KeyError:
            arr, indices = self._input_tensors(shape)
            self.inputs_buffer[ndim] = arr, indices
            return arr, indices

    def _output_tensor(self, arr, indices, axis):
        return take_along_axis(arr, indices, axis)

    def get_output_tensors(self, shape, axis):
        ndim = len(shape)
        try:
            return self.output_buffer[(ndim, axis)]
        except KeyError:
            arr, indices = self.get_input_tensors(shape)
            out = self._output_tensor(arr, indices, axis)
            self.output_buffer[(ndim, axis)] = out
            return out

    def _function(self, arr, indices, out):
        return aesara.function([arr, indices], [out])

    def get_function(self, shape, axis):
        ndim = len(shape)
        try:
            return self.func_buffer[(ndim, axis)]
        except KeyError:
            arr, indices = self.get_input_tensors(shape)
            out = self.get_output_tensors(shape, axis)
            func = self._function(arr, indices, out)
            self.func_buffer[(ndim, axis)] = func
            return func

    @staticmethod
    def get_input_values(shape, axis, samples):
        arr = np.random.randn(*shape).astype(FLOATX)
        size = list(shape)
        size[axis] = samples
        size = tuple(size)
        indices = np.random.randint(low=0, high=shape[axis], size=size, dtype=INTX)
        return arr, indices

    @pytest.mark.parametrize(
        ["shape", "axis", "samples"],
        product(
            [
                (1,),
                (3,),
                (3, 1),
                (3, 2),
                (1, 1),
                (1, 2),
                (40, 40),  # choose fails here
                (5, 1, 1),
                (5, 1, 2),
                (5, 3, 1),
                (5, 3, 2),
            ],
            [0, -1],
            [1, 10],
        ),
        ids=str,
    )
    def test_take_along_axis(self, shape, axis, samples):
        arr, indices = self.get_input_values(shape, axis, samples)
        func = self.get_function(shape, axis)
        assert np.allclose(np_take_along_axis(arr, indices, axis=axis), func(arr, indices)[0])

    @pytest.mark.parametrize(
        ["shape", "axis", "samples"],
        product(
            [
                (1,),
                (3,),
                (3, 1),
                (3, 2),
                (1, 1),
                (1, 2),
                (40, 40),  # choose fails here
                (5, 1, 1),
                (5, 1, 2),
                (5, 3, 1),
                (5, 3, 2),
            ],
            [0, -1],
            [1, 10],
        ),
        ids=str,
    )
    def test_take_along_axis_grad(self, shape, axis, samples):
        if axis < 0:
            _axis = len(shape) + axis
        else:
            _axis = axis
        # Setup the aesara function
        t_arr, t_indices = self.get_input_tensors(shape)
        t_out2 = aesara.grad(
            at.sum(self._output_tensor(t_arr ** 2, t_indices, axis)),
            t_arr,
        )
        func = aesara.function([t_arr, t_indices], [t_out2])

        # Test that the gradient gives the same output as what is expected
        arr, indices = self.get_input_values(shape, axis, samples)
        expected_grad = np.zeros_like(arr)
        slicer = [slice(None)] * len(shape)
        for i in range(indices.shape[axis]):
            slicer[axis] = i
            inds = indices[tuple(slicer)].reshape(shape[:_axis] + (1,) + shape[_axis + 1 :])
            inds = _make_along_axis_idx(shape, inds, _axis)
            expected_grad[inds] += 1
        expected_grad *= 2 * arr
        out = func(arr, indices)[0]
        assert np.allclose(out, expected_grad)

    @pytest.mark.parametrize("axis", [-4, 4], ids=str)
    def test_axis_failure(self, axis):
        arr, indices = self.get_input_tensors((3, 1))
        with pytest.raises(ValueError):
            take_along_axis(arr, indices, axis=axis)

    def test_ndim_failure(self):
        arr = TensorType(FLOATX, [False] * 3)("arr")
        indices = TensorType(INTX, [False] * 2)("indices")
        arr.tag.test_value = np.zeros((1,) * arr.ndim, dtype=FLOATX)
        indices.tag.test_value = np.zeros((1,) * indices.ndim, dtype=INTX)
        with pytest.raises(ValueError):
            take_along_axis(arr, indices)

    def test_dtype_failure(self):
        arr = TensorType(FLOATX, [False] * 3)("arr")
        indices = TensorType(FLOATX, [False] * 3)("indices")
        arr.tag.test_value = np.zeros((1,) * arr.ndim, dtype=FLOATX)
        indices.tag.test_value = np.zeros((1,) * indices.ndim, dtype=FLOATX)
        with pytest.raises(IndexError):
            take_along_axis(arr, indices)


def test_extract_obs_data():

    with pytest.raises(TypeError):
        extract_obs_data(at.matrix())

    data = np.random.normal(size=(2, 3))
    data_at = at.as_tensor(data)
    mask = np.random.binomial(1, 0.5, size=(2, 3)).astype(bool)

    for val_at in (data_at, aesara.shared(data)):
        res = extract_obs_data(val_at)

        assert isinstance(res, np.ndarray)
        assert np.array_equal(res, data)

    # AdvancedIncSubtensor check
    data_m = np.ma.MaskedArray(data, mask)
    missing_values = data_at.type()[mask]
    constant = at.as_tensor(data_m.filled())
    z_at = at.set_subtensor(constant[mask.nonzero()], missing_values)

    assert isinstance(z_at.owner.op, AdvancedIncSubtensor)

    res = extract_obs_data(z_at)

    assert isinstance(res, np.ndarray)
    assert np.ma.allequal(res, data_m)

    # AdvancedIncSubtensor1 check
    data = np.random.normal(size=(3,))
    data_at = at.as_tensor(data)
    mask = np.random.binomial(1, 0.5, size=(3,)).astype(bool)

    data_m = np.ma.MaskedArray(data, mask)
    missing_values = data_at.type()[mask]
    constant = at.as_tensor(data_m.filled())
    z_at = at.set_subtensor(constant[mask.nonzero()], missing_values)

    assert isinstance(z_at.owner.op, AdvancedIncSubtensor1)

    res = extract_obs_data(z_at)

    assert isinstance(res, np.ndarray)
    assert np.ma.allequal(res, data_m)


@pytest.mark.parametrize("input_dtype", ["int32", "int64", "float32", "float64"])
def test_pandas_to_array(input_dtype):
    """
    Ensure that pandas_to_array returns the dense array, masked array,
    graph variable, TensorVariable, or sparse matrix as appropriate.
    """
    # Create the various inputs to the function
    sparse_input = sps.csr_matrix(np.eye(3)).astype(input_dtype)
    dense_input = np.arange(9).reshape((3, 3)).astype(input_dtype)

    input_name = "input_variable"
    aesara_graph_input = at.as_tensor(dense_input, name=input_name)
    pandas_input = pd.DataFrame(dense_input)

    # All the even numbers are replaced with NaN
    missing_numpy_input = np.array([[np.nan, 1, np.nan], [3, np.nan, 5], [np.nan, 7, np.nan]])
    missing_pandas_input = pd.DataFrame(missing_numpy_input)
    masked_array_input = ma.array(dense_input, mask=(np.mod(dense_input, 2) == 0))

    # Create a generator object. Apparently the generator object needs to
    # yield numpy arrays.
    square_generator = (np.array([i ** 2], dtype=int) for i in range(100))

    # Alias the function to be tested
    func = pandas_to_array

    #####
    # Perform the various tests
    #####
    # Check function behavior with dense arrays and pandas dataframes
    # without missing values
    for input_value in [dense_input, pandas_input]:
        func_output = func(input_value)
        assert isinstance(func_output, np.ndarray)
        assert func_output.shape == input_value.shape
        npt.assert_allclose(func_output, dense_input)

    # Check function behavior with sparse matrix inputs
    sparse_output = func(sparse_input)
    assert sps.issparse(sparse_output)
    assert sparse_output.shape == sparse_input.shape
    npt.assert_allclose(sparse_output.toarray(), sparse_input.toarray())

    # Check function behavior when using masked array inputs and pandas
    # objects with missing data
    for input_value in [missing_numpy_input, masked_array_input, missing_pandas_input]:
        func_output = func(input_value)
        assert isinstance(func_output, ma.core.MaskedArray)
        assert func_output.shape == input_value.shape
        npt.assert_allclose(func_output, masked_array_input)

    # Check function behavior with Aesara graph variable
    aesara_output = func(aesara_graph_input)
    assert isinstance(aesara_output, Variable)
    npt.assert_allclose(aesara_output.eval(), aesara_graph_input.eval())
    intX = pm.aesaraf._conversion_map[aesara.config.floatX]
    if dense_input.dtype == intX or dense_input.dtype == aesara.config.floatX:
        assert aesara_output.owner is None  # func should not have added new nodes
        assert aesara_output.name == input_name
    else:
        assert aesara_output.owner is not None  # func should have casted
        assert aesara_output.owner.inputs[0].name == input_name

    if "float" in input_dtype:
        assert aesara_output.dtype == aesara.config.floatX
    else:
        assert aesara_output.dtype == intX

    # Check function behavior with generator data
    generator_output = func(square_generator)

    # Output is wrapped with `pm.floatX`, and this unwraps
    wrapped = generator_output.owner.inputs[0]
    # Make sure the returned object has .set_gen and .set_default methods
    assert hasattr(wrapped, "set_gen")
    assert hasattr(wrapped, "set_default")
    # Make sure the returned object is an Aesara TensorVariable
    assert isinstance(wrapped, TensorVariable)


def test_walk_model():
    d = at.vector("d")
    b = at.vector("b")
    c = uniform(0.0, d)
    c.name = "c"
    e = at.log(c)
    a = normal(e, b)
    a.name = "a"

    test_graph = at.exp(a + 1)
    res = list(walk_model((test_graph,)))
    assert a in res
    assert c not in res

    res = list(walk_model((test_graph,), walk_past_rvs=True))
    assert a in res
    assert c in res

    res = list(walk_model((test_graph,), walk_past_rvs=True, stop_at_vars={e}))
    assert a in res
    assert c not in res


def test_rvs_to_value_vars():

    with pm.Model() as m:
        a = pm.Uniform("a", 0.0, 1.0)
        b = pm.Uniform("b", 0, a + 1.0)
        c = pm.Normal("c")
        d = at.log(c + b) + 2.0

    a_value_var = m.rvs_to_values[a]
    assert a_value_var.tag.transform

    b_value_var = m.rvs_to_values[b]
    c_value_var = m.rvs_to_values[c]

    (res,), replaced = rvs_to_value_vars((d,))

    assert res.owner.op == at.add
    log_output = res.owner.inputs[0]
    assert log_output.owner.op == at.log
    log_add_output = res.owner.inputs[0].owner.inputs[0]
    assert log_add_output.owner.op == at.add
    c_output = log_add_output.owner.inputs[0]

    # We make sure that the random variables were replaced
    # with their value variables
    assert c_output == c_value_var
    b_output = log_add_output.owner.inputs[1]
    assert b_output == b_value_var

    res_ancestors = list(walk_model((res,), walk_past_rvs=True))
    res_rv_ancestors = [
        v for v in res_ancestors if v.owner and isinstance(v.owner.op, RandomVariable)
    ]

    # There shouldn't be any `RandomVariable`s in the resulting graph
    assert len(res_rv_ancestors) == 0
    assert b_value_var in res_ancestors
    assert c_value_var in res_ancestors
    assert a_value_var not in res_ancestors

    (res,), replaced = rvs_to_value_vars((d,), apply_transforms=True)

    res_ancestors = list(walk_model((res,), walk_past_rvs=True))
    res_rv_ancestors = [
        v for v in res_ancestors if v.owner and isinstance(v.owner.op, RandomVariable)
    ]

    assert len(res_rv_ancestors) == 0
    assert a_value_var in res_ancestors
    assert b_value_var in res_ancestors
    assert c_value_var in res_ancestors
