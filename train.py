"""Library file for executing training and evaluation on ogbg-molpcba."""

import os
from typing import Any, Dict, Iterable, Tuple, Optional

from absl import logging
from clu import checkpoint
from clu import metric_writers
from clu import metrics
from clu import parameter_overview
from clu import periodic_actions
import e3nn_jax as e3nn
import flax
import flax.core
import flax.linen as nn
from flax.training import train_state
import jax
import jax.numpy as jnp
import jraph
import ml_collections
import numpy as np
import optax
import tensorflow as tf

import datatypes
import input_pipeline
import models


@flax.struct.dataclass
class TrainMetrics(metrics.Collection):

    loss: metrics.Average.from_output("loss")
    log_likelihood: metrics.Average.from_output("log_likelihood")


@flax.struct.dataclass
class EvalMetrics(metrics.Collection):

    loss: metrics.Average.from_output("loss")
    log_likelihood: metrics.Average.from_output("log_likelihood")


def add_prefix_to_keys(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    """Adds a prefix to the keys of a dict, returning a new dict."""
    return {f"{prefix}_{key}": val for key, val in result.items()}


def create_model(config: ml_collections.ConfigDict, deterministic: bool) -> nn.Module:
    """Create a Flax model as specified by the config."""
    if config.model == "GraphNet":
        return models.GraphNet(
            latent_size=config.latent_size,
            num_mlp_layers=config.num_mlp_layers,
            message_passing_steps=config.message_passing_steps,
            output_nodes_size=config.num_classes,
            dropout_rate=config.dropout_rate,
            skip_connections=config.skip_connections,
            layer_norm=config.layer_norm,
            use_edge_model=config.use_edge_model,
            deterministic=deterministic,
        )
    if config.model == "GraphMLP":
        return models.GraphMLP(
            latent_size=config.latent_size,
            num_mlp_layers=config.num_mlp_layers,
            output_nodes_size=config.num_classes,
            dropout_rate=config.dropout_rate,
            layer_norm=config.layer_norm,
            deterministic=deterministic,
        )
    if config.model == "MACE":
        # TODO (ameyad): Implement MACE in Flax.
        raise NotImplementedError("MACE is not yet implemented.")

    raise ValueError(f"Unsupported model: {config.model}.")


def create_optimizer(config: ml_collections.ConfigDict) -> optax.GradientTransformation:
    """Create an optimizer as specified by the config."""
    if config.optimizer == "adam":
        return optax.adam(learning_rate=config.learning_rate)
    if config.optimizer == "sgd":
        return optax.sgd(learning_rate=config.learning_rate, momentum=config.momentum)
    raise ValueError(f"Unsupported optimizer: {config.optimizer}.")


def generation_loss(
    preds: datatypes.ModelOutput,
    graphs: jraph.GraphsTuple,
    res_beta,
    res_alpha,
    radius_rbf_variance=30,
):
    """Computes the loss for the generation task.
    Args:
        output (ModelOutput):
        graphs (jraph.GraphsTuple): a batch of graphs representing the current generated molecules
    """
    # TODO: ameyad Handle padding graphs with mask.

    num_radii = RADII.shape[0]
    num_graphs = graphs.n_node.shape[0]
    num_nodes = graphs.nodes.shape[0]
    num_elements = ELEMENTS.shape[0]

    # The true focus is the first element of each subgraph.
    # Compute indices for the true focus in each subgraph of the batch.
    true_focus_indices = jnp.concatenate(
        [jnp.array([0]), jnp.cumsum(graphs.n_node[:-1])]
    )

    def focus_loss():
        # focus_logits is of shape (num_nodes,)
        assert preds.focus_logits.shape == (num_nodes,)
        assert graphs.globals["focus_distribution"] == (num_nodes,)
        jnp.sum(-preds.focus_logits * graphs.globals["focus_distribution"])
        return jnp.sum(
            -preds.focus_logits[true_focus_indices]
        ) + jax.scipy.special.logsumexp(preds.focus_logits)

    jraph.segment_softmax()
    all_focus_probs = jax.nn.softmax(preds.focus_logits)
    correct_focus_probs = all_focus_probs[true_focus_indices]

    e3nn.scatter_sum(graphs.nodes, nel=graphs.n_node)

    def atom_type_loss():
        # atom_type_logits is of shape (num_graphs, num_elements)
        assert graphs.globals["atom_type_distribution"] == (num_graphs, num_elements)
        assert preds.atom_type_logits.shape == (num_graphs, num_elements)

        return optax.softmax_cross_entropy(
            graphs.globals["atom_type_distribution"], preds.atom_type_logits
        ).mean()

    def position_loss():
        # position_coeffs is an e3nn.IrrepsArray of shape (num_graphs, num_radii, dim(irreps))
        assert preds.position_coeffs == (
            num_graphs,
            num_radii,
            preds.position_coeffs.irreps.dim,
        )

        # Integrate the position signal over each sphere to get the probability distribution over the radii.
        position_signal = e3nn.to_s2grid(
            preds.position_coeffs,
            res_beta,
            res_alpha,
            quadrature="gausslegendre",
            normalization="integral",
        )

        # position_signal is of shape (# of graphs, # of radii, res_beta, res_alpha)
        assert position_signal.shape == (num_graphs, num_radii, res_beta, res_alpha)

        # For numerical stability, we subtract out the maximum value over each sphere before exponentiating.
        position_max = jnp.max(position_signal, axis=(-3, -2, -1), keepdims=True)
        sphere_normalizing_factors = position_signal.apply(
            lambda pos: jnp.exp(pos - position_max)
        ).integrate()

        # sphere_normalizing_factors is of shape (num_graphs, num_radii)
        assert sphere_normalizing_factors.shape == (num_graphs, num_radii)

        # Compare distance of target relative to focus to target_radius.
        target_positions = graphs.globals["target_positions"]
        assert target_positions.shape == (num_graphs, 3)

        # Get radius weights from the "true" distribution, described by a RBF kernel around the target positions.
        radius_weights = jax.vmap(
            lambda target_position: jax.vmap(
                lambda radius: jnp.exp(
                    -((radius - jnp.linalg.norm(target_position)) ** 2)
                    / (2 * radius_rbf_variance)
                )
            )(RADII)
        )(target_positions)
        radius_weights = radius_weights / jnp.sum(
            radius_weights, axis=-1, keepdims=True
        )

        # radius_weights is of shape (num_graphs, num_radii)
        assert radius_weights.shape == (num_graphs, num_radii)

        # Compute f(r*, rhat*) which is our model predictions for the target positions.
        target_positions_logits = e3nn.to_s2point(
            preds.position_coeffs, target_positions, normalization="integral"
        )
        assert target_positions_logits.shape == (num_graphs, num_radii)
        loss_positions = jax.vmap(
            lambda fr, qr, Zr, c: -jnp.sum(qr * fr) + jnp.log(jnp.sum(Zr)) + c
        )(
            target_positions_logits,
            radius_weights,
            sphere_normalizing_factors,
            position_max,
        )

        assert loss_positions.shape == (num_graphs,)
        return jnp.mean(loss_positions)

    total_loss = focus_loss() + (1 - preds.stop) * correct_focus_probs * (
        loss_type + correct_type_probs * position_loss()
    )
    return total_loss


def replace_globals(graphs: jraph.GraphsTuple) -> jraph.GraphsTuple:
    """Replaces the globals attribute with a constant feature for each graph."""
    return graphs._replace(globals=jnp.ones([graphs.n_node.shape[0], 1]))


def get_predicted_logits(
    state: train_state.TrainState,
    graphs: jraph.GraphsTuple,
    rngs: Optional[Dict[str, jnp.ndarray]],
) -> jnp.ndarray:
    """Get predicted logits from the network for input graphs."""
    pred_graphs = state.apply_fn(state.params, graphs, rngs=rngs)
    logits = pred_graphs.globals
    return logits


def get_valid_mask(labels: jnp.ndarray, graphs: jraph.GraphsTuple) -> jnp.ndarray:
    """Gets the binary mask indicating only valid labels and graphs."""
    # We have to ignore all NaN values - which indicate labels for which
    # the current graphs have no label.
    labels_mask = ~jnp.isnan(labels)

    # Since we have extra 'dummy' graphs in our batch due to padding, we want
    # to mask out any loss associated with the dummy graphs.
    # Since we padded with `pad_with_graphs` we can recover the mask by using
    # get_graph_padding_mask.
    graph_mask = jraph.get_graph_padding_mask(graphs)

    # Combine the mask over labels with the mask over graphs.
    return labels_mask & graph_mask[:, None]


@jax.jit
def train_step(
    state: train_state.TrainState,
    graphs: jraph.GraphsTuple,
    rngs: Dict[str, jnp.ndarray],
) -> Tuple[train_state.TrainState, metrics.Collection]:
    """Performs one update step over the current batch of graphs."""

    def loss_fn(params, graphs):
        curr_state = state.replace(params=params)

        # Extract labels.
        labels = graphs.globals

        # Replace the global feature for graph classification.
        graphs = replace_globals(graphs)

        # Compute logits and resulting loss.
        logits = get_predicted_logits(curr_state, graphs, rngs)
        mask = get_valid_mask(labels, graphs)
        loss = generation_loss(logits=logits, labels=labels, mask=mask)
        mean_loss = jnp.sum(jnp.where(mask, loss, 0)) / jnp.sum(mask)

        return mean_loss, (loss, logits, labels, mask)

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (_, (loss, logits, labels, mask)), grads = grad_fn(state.params, graphs)
    state = state.apply_gradients(grads=grads)

    metrics_update = TrainMetrics.single_from_model_output(
        loss=loss, logits=logits, labels=labels, mask=mask
    )
    return state, metrics_update


@jax.jit
def evaluate_step(
    state: train_state.TrainState,
    graphs: jraph.GraphsTuple,
) -> metrics.Collection:
    """Computes metrics over a set of graphs."""

    # The target labels our model has to predict.
    labels = graphs.globals

    # Replace the global feature for graph classification.
    graphs = replace_globals(graphs)

    # Get predicted logits, and corresponding probabilities.
    logits = get_predicted_logits(state, graphs, rngs=None)

    # Get the mask for valid labels and graphs.
    mask = get_valid_mask(labels, graphs)

    # Compute the various metrics.
    loss = loss(logits=logits, labels=labels, mask=mask)

    return EvalMetrics.single_from_model_output(
        loss=loss, logits=logits, labels=labels, mask=mask
    )


def evaluate_model(
    state: train_state.TrainState,
    datasets: Dict[str, tf.data.Dataset],
    splits: Iterable[str],
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics = {}
    for split in splits:
        split_metrics = None

        # Loop over graphs.
        for graphs in datasets[split].as_numpy_iterator():
            split_metrics_update = evaluate_step(state, graphs)

            # Update metrics.
            if split_metrics is None:
                split_metrics = split_metrics_update
            else:
                split_metrics = split_metrics.merge(split_metrics_update)
        eval_metrics[split] = split_metrics

    return eval_metrics  # pytype: disable=bad-return-type


def train_and_evaluate(
    config: ml_collections.ConfigDict, workdir: str, key: jax.random.PRNGKey
) -> train_state.TrainState:
    """Execute model training and evaluation loop.

    Args:
      config: Hyperparameter configuration for training and evaluation.
      workdir: Directory where the TensorBoard summaries are written to.

    Returns:
      The train state (which includes the `.params`).
    """
    # We only support single-host training.
    assert jax.process_count() == 1

    # Create writer for logs.
    writer = metric_writers.create_default_writer(workdir)
    writer.write_hparams(config.to_dict())

    # Get datasets, organized by split.
    logging.info("Obtaining datasets.")
    datasets = input_pipeline.get_datasets(key, config.batch_size)
    train_iter = iter(datasets["train"])

    # Create and initialize the network.
    logging.info("Initializing network.")
    rng = jax.random.PRNGKey(0)
    rng, init_rng = jax.random.split(rng)
    init_graphs = next(datasets["train"].as_numpy_iterator())
    init_graphs = replace_globals(init_graphs)
    init_net = create_model(config, deterministic=True)
    params = jax.jit(init_net.init)(init_rng, init_graphs)
    parameter_overview.log_parameter_overview(params)

    # Create the optimizer.
    tx = create_optimizer(config)

    # Create the training state.
    net = create_model(config, deterministic=False)
    state = train_state.TrainState.create(apply_fn=net.apply, params=params, tx=tx)

    # Set up checkpointing of the model.
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=2)
    state = ckpt.restore_or_initialize(state)
    initial_step = int(state.step) + 1

    # Create the evaluation state, corresponding to a deterministic model.
    eval_net = create_model(config, deterministic=True)
    eval_state = state.replace(apply_fn=eval_net.apply)

    # Hooks called periodically during training.
    report_progress = periodic_actions.ReportProgress(
        num_train_steps=config.num_train_steps, writer=writer
    )
    profiler = periodic_actions.Profile(num_profile_steps=5, logdir=workdir)
    hooks = [report_progress, profiler]

    # Begin training loop.
    logging.info("Starting training.")
    train_metrics = None
    for step in range(initial_step, config.num_train_steps + 1):

        # Split PRNG key, to ensure different 'randomness' for every step.
        rng, dropout_rng = jax.random.split(rng)

        # Perform one step of training.
        with jax.profiler.StepTraceAnnotation("train", step_num=step):
            graphs = jax.tree_util.tree_map(np.asarray, next(train_iter))
            state, metrics_update = train_step(
                state, graphs, rngs={"dropout": dropout_rng}
            )

            # Update metrics.
            if train_metrics is None:
                train_metrics = metrics_update
            else:
                train_metrics = train_metrics.merge(metrics_update)

        # Quick indication that training is happening.
        logging.log_first_n(logging.INFO, "Finished training step %d.", 10, step)
        for hook in hooks:
            hook(step)

        # Log, if required.
        is_last_step = step == config.num_train_steps
        if step % config.log_every_steps == 0 or is_last_step:
            writer.write_scalars(
                step, add_prefix_to_keys(train_metrics.compute(), "train")
            )
            train_metrics = None

        # Evaluate on validation and test splits, if required.
        if step % config.eval_every_steps == 0 or is_last_step:
            eval_state = eval_state.replace(params=state.params)

            splits = ["validation", "test"]
            with report_progress.timed("eval"):
                eval_metrics = evaluate_model(eval_state, datasets, splits=splits)
            for split in splits:
                writer.write_scalars(
                    step, add_prefix_to_keys(eval_metrics[split].compute(), split)
                )

        # Checkpoint model, if required.
        if step % config.checkpoint_every_steps == 0 or is_last_step:
            with report_progress.timed("checkpoint"):
                ckpt.save(state)

    return state
