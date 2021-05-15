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

import aesara
import numpy as np
import pytest

from aesara import tensor as at

import pymc3 as pm

from pymc3.distributions.distribution import _validate_shape_dims_size
from pymc3.distributions.shape_utils import (
    broadcast_dist_samples_shape,
    broadcast_dist_samples_to,
    broadcast_distribution_samples,
    get_broadcastable_dist_samples,
    shapes_broadcasting,
    to_tuple,
)
from pymc3.exceptions import ShapeWarning

test_shapes = [
    (tuple(), (1,), (4,), (5, 4)),
    (tuple(), (1,), (7,), (5, 4)),
    (tuple(), (1,), (1, 4), (5, 4)),
    (tuple(), (1,), (5, 1), (5, 4)),
    (tuple(), (1,), (3, 4), (5, 4)),
    (tuple(), (1,), (5, 3), (5, 4)),
    (tuple(), (1,), (10, 4), (5, 4)),
    (tuple(), (1,), (10,), (5, 4)),
    (tuple(), (1,), (1, 1, 4), (5, 4)),
    (tuple(), (1,), (10, 1, 4), (5, 4)),
    (tuple(), (1,), (10, 5, 4), (5, 4)),
]
test_sizes = [
    None,
    tuple(),
    1,
    (1,),
    10,
    (10,),
    (1, 1),
    (10, 1),
    (1, 10),
    (5,),
    (5, 4),
    (1, 1, 1, 1),
]
test_to_shapes = [None, tuple(), (10, 5, 4), (10, 1, 1, 5, 1)]


@pytest.fixture(params=test_sizes, ids=str)
def fixture_sizes(request):
    return request.param


@pytest.fixture(params=test_shapes, ids=str)
def fixture_shapes(request):
    return request.param


@pytest.fixture(params=[False, True], ids=str)
def fixture_exception_handling(request):
    return request.param


@pytest.fixture()
def samples_to_broadcast(fixture_sizes, fixture_shapes):
    samples = [np.empty(s) for s in fixture_shapes]
    try:
        broadcast_shape = broadcast_dist_samples_shape(fixture_shapes, size=fixture_sizes)
    except ValueError:
        broadcast_shape = None
    return fixture_sizes, samples, broadcast_shape


@pytest.fixture(params=test_to_shapes, ids=str)
def samples_to_broadcast_to(request, samples_to_broadcast):
    to_shape = request.param
    size, samples, broadcast_shape = samples_to_broadcast
    if broadcast_shape is not None:
        try:
            broadcast_shape = broadcast_dist_samples_shape(
                [broadcast_shape, to_tuple(to_shape)], size=size
            )
        except ValueError:
            broadcast_shape = None
    return to_shape, size, samples, broadcast_shape


@pytest.fixture
def fixture_model():
    with pm.Model() as model:
        n = 5
        dim = 4
        with pm.Model():
            cov = pm.InverseGamma("cov", alpha=1, beta=1)
            x = pm.Normal("x", mu=np.ones((dim,)), sigma=pm.math.sqrt(cov), shape=(n, dim))
            eps = pm.HalfNormal("eps", np.ones((n, 1)), shape=(n, dim))
            mu = pm.Deterministic("mu", at.sum(x + eps, axis=-1))
            y = pm.Normal("y", mu=mu, sigma=1, shape=(n,))
    return model, [cov, x, eps, y]


class TestShapesBroadcasting:
    @pytest.mark.parametrize(
        "bad_input",
        [None, [None], "asd", 3.6, {1: 2}, {3}, [8, [8]], "3", ["3"], np.array([[2]])],
        ids=str,
    )
    def test_type_check_raises(self, bad_input):
        with pytest.raises(TypeError):
            shapes_broadcasting(bad_input, tuple(), raise_exception=True)
        with pytest.raises(TypeError):
            shapes_broadcasting(bad_input, tuple(), raise_exception=False)

    def test_type_check_success(self):
        inputs = [3, 3.0, tuple(), [3], (3,), np.array(3), np.array([3])]
        out = shapes_broadcasting(*inputs)
        assert out == (3,)

    def test_broadcasting(self, fixture_shapes, fixture_exception_handling):
        shapes = fixture_shapes
        raise_exception = fixture_exception_handling
        try:
            expected_out = np.broadcast(*[np.empty(s) for s in shapes]).shape
        except ValueError:
            expected_out = None
        if expected_out is None:
            if raise_exception:
                with pytest.raises(ValueError):
                    shapes_broadcasting(*shapes, raise_exception=raise_exception)
            else:
                out = shapes_broadcasting(*shapes, raise_exception=raise_exception)
                assert out is None
        else:
            out = shapes_broadcasting(*shapes, raise_exception=raise_exception)
            assert out == expected_out

    def test_broadcast_dist_samples_shape(self, fixture_sizes, fixture_shapes):
        size = fixture_sizes
        shapes = fixture_shapes
        size_ = to_tuple(size)
        shapes_ = [
            s if s[: min([len(size_), len(s)])] != size_ else s[len(size_) :] for s in shapes
        ]
        try:
            expected_out = np.broadcast(*[np.empty(s) for s in shapes_]).shape
        except ValueError:
            expected_out = None
        if expected_out is not None and any(
            s[: min([len(size_), len(s)])] == size_ for s in shapes
        ):
            expected_out = size_ + expected_out
        if expected_out is None:
            with pytest.raises(ValueError):
                broadcast_dist_samples_shape(shapes, size=size)
        else:
            out = broadcast_dist_samples_shape(shapes, size=size)
            assert out == expected_out


class TestSamplesBroadcasting:
    def test_broadcast_distribution_samples(self, samples_to_broadcast):
        size, samples, broadcast_shape = samples_to_broadcast
        if broadcast_shape is not None:
            outs = broadcast_distribution_samples(samples, size=size)
            assert all(o.shape == broadcast_shape for o in outs)
        else:
            with pytest.raises(ValueError):
                broadcast_distribution_samples(samples, size=size)

    def test_get_broadcastable_dist_samples(self, samples_to_broadcast):
        size, samples, broadcast_shape = samples_to_broadcast
        if broadcast_shape is not None:
            size_ = to_tuple(size)
            outs, out_shape = get_broadcastable_dist_samples(
                samples, size=size, return_out_shape=True
            )
            assert out_shape == broadcast_shape
            for i, o in zip(samples, outs):
                ishape = i.shape
                if ishape[: min([len(size_), len(ishape)])] == size_:
                    expected_shape = (
                        size_ + (1,) * (len(broadcast_shape) - len(ishape)) + ishape[len(size_) :]
                    )
                else:
                    expected_shape = ishape
                assert o.shape == expected_shape
            assert shapes_broadcasting(*[o.shape for o in outs]) == broadcast_shape
        else:
            with pytest.raises(ValueError):
                get_broadcastable_dist_samples(samples, size=size)

    def test_broadcast_dist_samples_to(self, samples_to_broadcast_to):
        to_shape, size, samples, broadcast_shape = samples_to_broadcast_to
        if broadcast_shape is not None:
            outs = broadcast_dist_samples_to(to_shape, samples, size=size)
            assert all(o.shape == broadcast_shape for o in outs)
        else:
            with pytest.raises(ValueError):
                broadcast_dist_samples_to(to_shape, samples, size=size)


@pytest.mark.xfail(reason="InverseGamma was not yet refactored")
def test_sample_generate_values(fixture_model, fixture_sizes):
    model, RVs = fixture_model
    size = to_tuple(fixture_sizes)
    with model:
        prior = pm.sample_prior_predictive(samples=fixture_sizes)
        for rv in RVs:
            assert prior[rv.name].shape == size + tuple(rv.distribution.shape)


class TestShapeDimsSize:
    @pytest.mark.parametrize("param_shape", [(), (3,)])
    @pytest.mark.parametrize("batch_shape", [(), (3,)])
    @pytest.mark.parametrize(
        "parametrization",
        [
            "implicit",
            "shape",
            # "shape...",
            "dims",
            "dims...",
            "size",
        ],
    )
    def test_param_and_batch_shape_combos(
        self, param_shape: tuple, batch_shape: tuple, parametrization: str
    ):
        coords = {}
        param_dims = []
        batch_dims = []

        # Create coordinates corresponding to the parameter shape
        for d in param_shape:
            dname = f"param_dim_{d}"
            coords[dname] = [f"c_{i}" for i in range(d)]
            param_dims.append(dname)
        assert len(param_dims) == len(param_shape)
        # Create coordinates corresponding to the batch shape
        for d in batch_shape:
            dname = f"batch_dim_{d}"
            coords[dname] = [f"c_{i}" for i in range(d)]
            batch_dims.append(dname)
        assert len(batch_dims) == len(batch_shape)

        with pm.Model(coords=coords) as pmodel:
            mu = aesara.shared(np.random.normal(size=param_shape))

            with pytest.warns(None):
                if parametrization == "implicit":
                    rv = pm.Normal("rv", mu=mu).shape == param_shape
                else:
                    expected_shape = batch_shape + param_shape
                    if parametrization == "shape":
                        rv = pm.Normal("rv", mu=mu, shape=batch_shape + param_shape)
                        assert rv.eval().shape == expected_shape
                    # elif parametrization == "shape...":
                    #     rv = pm.Normal("rv", mu=mu, shape=(*batch_shape, ...))
                    #     assert rv.eval().shape == batch_shape + param_shape
                    elif parametrization == "dims":
                        rv = pm.Normal("rv", mu=mu, dims=batch_dims + param_dims)
                        assert rv.eval().shape == expected_shape
                    elif parametrization == "dims...":
                        rv = pm.Normal("rv", mu=mu, dims=(*batch_dims, ...))
                        n_size = len(batch_shape)
                        n_implied = len(param_shape)
                        ndim = n_size + n_implied
                        assert len(pmodel.RV_dims["rv"]) == ndim, pmodel.RV_dims
                        assert len(pmodel.RV_dims["rv"][:n_size]) == len(batch_dims)
                        assert len(pmodel.RV_dims["rv"][n_size:]) == len(param_dims)
                        if n_implied > 0:
                            assert pmodel.RV_dims["rv"][-1] is None
                    elif parametrization == "size":
                        rv = pm.Normal("rv", mu=mu, size=batch_shape + param_shape)
                        assert rv.eval().shape == expected_shape
                    else:
                        raise NotImplementedError("Invalid test case parametrization.")

    def test_define_dims_on_the_fly(self):
        with pm.Model() as pmodel:
            agedata = aesara.shared(np.array([10, 20, 30]))

            # Associate the "patient" dim with an implied dimension
            age = pm.Normal("age", agedata, dims=("patient",))
            assert "patient" in pmodel.dim_lengths
            assert pmodel.dim_lengths["patient"].eval() == 3

            # Use the dim to replicate a new RV
            effect = pm.Normal("effect", 0, dims=("patient",))
            assert effect.ndim == 1
            assert effect.eval().shape == (3,)

            # Now change the length of the implied dimension
            agedata.set_value([1, 2, 3, 4])
            # The change should propagate all the way through
            assert effect.eval().shape == (4,)

    @pytest.mark.xfail(reason="Simultaneous use of size and dims is not implemented")
    def test_data_defined_size_dimension_can_register_dimname(self):
        with pm.Model() as pmodel:
            x = pm.Data("x", [[1, 2, 3, 4]], dims=("first", "second"))
            assert "first" in pmodel.dim_lengths
            assert "second" in pmodel.dim_lengths
            # two dimensions are implied; a "third" dimension is created
            y = pm.Normal("y", mu=x, size=2, dims=("third", "first", "second"))
            assert "third" in pmodel.dim_lengths
            assert y.eval().shape() == (2, 1, 4)

    def test_can_resize_data_defined_size(self):
        with pm.Model() as pmodel:
            x = pm.Data("x", [[1, 2, 3, 4]], dims=("first", "second"))
            y = pm.Normal("y", mu=0, dims=("first", "second"))
            z = pm.Normal("z", mu=y, observed=np.ones((1, 4)))
            assert x.eval().shape == (1, 4)
            assert y.eval().shape == (1, 4)
            assert z.eval().shape == (1, 4)
            assert "first" in pmodel.dim_lengths
            assert "second" in pmodel.dim_lengths
            pmodel.set_data("x", [[1, 2], [3, 4], [5, 6]])
            assert x.eval().shape == (3, 2)
            assert y.eval().shape == (3, 2)
            assert z.eval().shape == (3, 2)

    @pytest.mark.xfail(reason="https://github.com/pymc-devs/aesara/issues/390")
    def test_size32_doesnt_break_broadcasting():
        size32 = at.constant([1, 10], dtype="int32")
        rv = pm.Normal.dist(0, 1, size=size32)
        assert rv.broadcastable == (True, False)

    def test_observed_with_column_vector(self):
        with pm.Model() as model:
            # The `observed` is a broadcastable column vector
            obs = at.as_tensor_variable(np.ones((3, 1), dtype=aesara.config.floatX))
            assert obs.broadcastable == (False, True)

    @pytest.mark.xfail(reason="https://github.com/pymc-devs/aesara/issues/390")
    def test_observed_with_column_vector(self):
        with pm.Model() as model:
            # The `observed` is a broadcastable column vector
            obs = at.as_tensor_variable(np.ones((3, 1), dtype=aesara.config.floatX))
            assert obs.broadcastable == (False, True)

            # Both shapes describe broadcastable volumn vectors
            size64 = at.constant([3, 1], dtype="int64")
            # But the second shape is upcasted from an int32 vector
            cast64 = at.cast(at.constant([3, 1], dtype="int32"), dtype="int64")

            pm.Normal("size64", mu=0, sd=1, size=size64, observed=obs)
            pm.Normal("shape64", mu=0, sd=1, shape=size64, observed=obs)
            model.logp()

            pm.Normal("size_cast64", mu=0, sd=1, size=cast64, observed=obs)
            pm.Normal("shape_cast64", mu=0, sd=1, shape=cast64, observed=obs)
            model.logp()

    def test_dist_api_works(self):
        mu = aesara.shared(np.array([1, 2, 3]))
        with pytest.raises(NotImplementedError, match="API is not supported"):
            pm.Normal.dist(mu=mu, dims=("town",))
        assert pm.Normal.dist(mu=mu, shape=(3,)).eval().shape == (3,)
        assert pm.Normal.dist(mu=mu, shape=(5, 3)).eval().shape == (5, 3)
        # assert pm.Normal.dist(mu=mu, shape=(7, ...)).eval().shape == (7, 3)
        assert pm.Normal.dist(mu=mu, size=(3,)).eval().shape == (3,)
        assert pm.Normal.dist(mu=mu, size=(4, 3)).eval().shape == (4, 3)

    def test_mvnormal_shape_size_difference(self):
        # Parameters add one batch dimension (4), shape is what you'd expect.
        # Under the hood the shape(4, 3) becomes size=(4,) and the RV is initially
        # created as (4, 4, 3). The internal ndim-check then recreates it with size=None.
        rv = pm.MvNormal.dist(mu=np.ones((4, 3)), cov=np.eye(3), shape=(4, 3))
        assert rv.ndim == 2
        assert tuple(rv.shape.eval()) == (4, 3)

        # shape adds two dimensions (5, 4)
        # Under the hood the shape=(5, 4, 3) becomes size=(5, 4).
        # The RV is created as (5, 4, 3) right away.
        rv = pm.MvNormal.dist(mu=[1, 2, 3], cov=np.eye(3), shape=(5, 4, 3))
        assert rv.ndim == 3
        assert tuple(rv.shape.eval()) == (5, 4, 3)

        # parameters add 1 batch dimension (4), shape adds another (5)
        # Under the hood the shape=(5, 4, 3) becomes size=(5, 4)
        # The RV is initially created as (5, 4, 3, 4, 3) and then recreated and resized.
        rv = pm.MvNormal.dist(mu=np.ones((4, 3)), cov=np.eye(3), shape=(5, 4, 3))
        assert rv.ndim == 3
        assert tuple(rv.shape.eval()) == (5, 4, 3)

        # rv = pm.MvNormal.dist(mu=np.ones((4, 3, 2)), cov=np.eye(2), shape=(6, 5, ...))
        # assert rv.ndim == 5
        # assert tuple(rv.shape.eval()) == (6, 5, 4, 3, 2)

        with pytest.warns(None):
            rv = pm.MvNormal.dist(mu=[1, 2, 3], cov=np.eye(3), size=(5, 4))
            assert tuple(rv.shape.eval()) == (5, 4, 3)

        # When using `size` the API behaves like Aesara/NumPy
        with pytest.warns(
            ShapeWarning,
            match=r"You may have expected a \(2\+1\)-dimensional RV, but the resulting RV will be 5-dimensional",
        ):
            rv = pm.MvNormal.dist(mu=np.ones((5, 4, 3)), cov=np.eye(3), size=(5, 4))
            assert tuple(rv.shape.eval()) == (5, 4, 5, 4, 3)

    def test_lazy_flavors(self):

        _validate_shape_dims_size(shape=5)
        _validate_shape_dims_size(dims="town")
        _validate_shape_dims_size(size=7)

        assert pm.Uniform.dist(2, [4, 5], size=[3, 2]).eval().shape == (3, 2)
        assert pm.Uniform.dist(2, [4, 5], shape=[3, 2]).eval().shape == (3, 2)
        with pm.Model(coords=dict(town=["Greifswald", "Madrid"])):
            assert pm.Normal("n2", mu=[1, 2], dims=("town",)).eval().shape == (2,)

    def test_invalid_flavors(self):
        # redundant parametrizations
        with pytest.raises(ValueError, match="Passing both"):
            _validate_shape_dims_size(shape=(2,), dims=("town",))
        with pytest.raises(ValueError, match="Passing both"):
            _validate_shape_dims_size(dims=("town",), size=(2,))
        with pytest.raises(ValueError, match="Passing both"):
            _validate_shape_dims_size(shape=(3,), size=(3,))

        # invalid, but not necessarly rare
        with pytest.raises(ValueError, match="must be an int, list or tuple"):
            _validate_shape_dims_size(size="notasize")

        # invalid ellipsis positions
        with pytest.raises(ValueError, match="may only appear in the last position"):
            _validate_shape_dims_size(shape=(3, ..., 2))
        # with pytest.raises(ValueError, match="may only appear in the last position"):
        #    _validate_shape_dims_size(dims=(..., "town"))
        with pytest.raises(ValueError, match="cannot contain"):
            _validate_shape_dims_size(size=(3, ...))
