import distrax
import haiku as hk
import jax
import jax.numpy as jnp
import e3nn_jax as e3nn

from symphony.models.radius_predictors import RadiusPredictor


class RationalQuadraticSplineRadialPredictor(RadiusPredictor):
    """A rational quadratic spline flow for the radial component."""

    def __init__(
        self,
        num_bins: int,
        min_radius: float,
        max_radius: float,
        num_layers: int,
        num_param_mlp_layers: int,
        boundary_error: float,
    ):
        super().__init__()
        self.num_bins = num_bins
        self.min_radius = min_radius
        self.max_radius = max_radius
        self.num_layers = num_layers
        self.num_param_mlp_layers = num_param_mlp_layers
        self.boundary_error = boundary_error
        self.boundary_error_max_tries = 3

    def create_flow(self, conditioning: e3nn.IrrepsArray) -> distrax.Bijector:
        """Creates a flow with the given conditioning."""
        conditioning = conditioning.filter("0e")
        conditioning = conditioning.array

        layers = []
        for _ in range(self.num_layers):
            param_dims = self.num_bins * 3 + 1
            params = hk.nets.MLP(
                [param_dims] * self.num_param_mlp_layers,
                activate_final=False,
                w_init=hk.initializers.RandomNormal(1e-4),
                b_init=hk.initializers.RandomNormal(1e-4),
            )(conditioning)
            layer = distrax.RationalQuadraticSpline(
                params,
                self.min_radius,
                self.max_radius,
                boundary_slopes="unconstrained",
                min_bin_size=1e-2,
            )
            layers.append(layer)

        flow = distrax.Inverse(distrax.Chain(layers))
        return flow

    def create_distribution(
        self, conditioning: e3nn.IrrepsArray
    ) -> distrax.Distribution:
        """Creates a distribution by composing a base distribution with a flow."""
        flow = self.create_flow(conditioning)
        base_distribution = distrax.Independent(
            distrax.Uniform(low=self.min_radius, high=self.max_radius),
            reinterpreted_batch_ndims=0,
        )
        dist = distrax.Transformed(base_distribution, flow)
        return dist

    def log_prob(
        self, samples: e3nn.IrrepsArray, conditioning: e3nn.IrrepsArray, eps: float = 1e-6
    ) -> jnp.ndarray:
        """Computes the log probability of the given samples."""
        dist = self.create_distribution(conditioning)
        radii = jnp.linalg.norm(samples.array, axis=-1)
        return dist.log_prob(radii)

    def sample(
        self,
        conditioning: e3nn.IrrepsArray,
    ) -> jnp.ndarray:
        """Samples from the learned distribution, ignoring samples near the boundaries."""
        dist = self.create_distribution(conditioning)
        rng, sample_rng = jax.random.split(hk.next_rng_key())
        sample_rngs = jax.random.split(sample_rng, self.boundary_error_max_tries)
        samples = jax.vmap(lambda rng: dist.sample(seed=rng))(sample_rngs)

        # return jax.random.choice(rng, samples)

        valid_range = samples >= self.min_radius + self.boundary_error
        valid_range = jnp.logical_and(
            valid_range, samples <= self.max_radius - self.boundary_error
        )

        return jax.random.choice(rng, samples, p=valid_range)