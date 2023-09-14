"""Library file for executing the training and evaluation of generative models."""

import functools
import os
import pickle
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union
import chex
import flax
import jax
import jax.numpy as jnp
import jraph
import ml_collections
import optax
import yaml
from absl import logging

from clu import (
    checkpoint,
    metric_writers,
    metrics,
    parameter_overview,
    periodic_actions,
)
from flax.training import train_state

from symphony import datatypes, models, loss
from symphony.data import input_pipeline_tf
from symphony import train


@flax.struct.dataclass
class Metrics(metrics.Collection):
    total_loss: metrics.Average.from_output("total_loss")
    denoising_loss: metrics.Average.from_output("denoising_loss")


@functools.partial(jax.jit, static_argnames=["add_noise_to_positions"])
def train_step(
    state: train_state.TrainState,
    graphs: datatypes.Fragments,
    rng: chex.PRNGKey,
    add_noise_to_positions: bool,
    noise_std: float,
) -> Tuple[train_state.TrainState, metrics.Collection]:
    """Performs one update step over the current batch of graphs."""

    def loss_fn(params: optax.Params, graphs: datatypes.Fragments, position_noise: Optional[jnp.ndarray]) -> float:
        curr_state = state.replace(params=params)
        preds = train.get_predictions(curr_state, graphs, rng=None)
        total_loss, (
           denoising_loss, _
        ) = loss.denoising_loss(
            preds=preds, graphs=graphs, position_noise=position_noise,
        )
        mask = jraph.get_graph_padding_mask(graphs)
        mean_loss = jnp.sum(jnp.where(mask, total_loss, 0.0)) / jnp.sum(mask)
        return mean_loss, (
            total_loss,
            denoising_loss,
            mask,
        )

    # Add noise to positions, if required.
    if add_noise_to_positions:
        noise_rng, rng = jax.random.split(rng)
        position_noise = (
            jax.random.normal(noise_rng, graphs.nodes.positions.shape) * noise_std
        )
        noisy_positions = graphs.nodes.positions + position_noise
        graphs = graphs._replace(nodes=graphs.nodes._replace(positions=noisy_positions))
    else:
        position_noise = None

    # Compute gradients.
    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (
        _,
        (total_loss, denoising_loss, mask),
    ), grads = grad_fn(state.params, graphs, position_noise)
    state = state.apply_gradients(grads=grads)

    batch_metrics = Metrics.single_from_model_output(
        total_loss=total_loss,
        denoising_loss=denoising_loss,
        mask=mask,
    )
    return state, batch_metrics


def evaluate_step(
    eval_state: train_state.TrainState,
    graphs: datatypes.Fragments,
    rng: chex.PRNGKey,
    add_noise_to_positions: bool,
    noise_std: float,
) -> metrics.Collection:
    """Computes metrics over a set of graphs."""
    if add_noise_to_positions:
        noise_rng, rng = jax.random.split(rng)
        position_noise = (
            jax.random.normal(noise_rng, graphs.nodes.positions.shape) * noise_std
        )
        noisy_positions = graphs.nodes.positions + position_noise
        graphs = graphs._replace(nodes=graphs.nodes._replace(positions=noisy_positions))
    else:
        position_noise = None

    # Compute predictions and resulting loss.
    preds = train.get_predictions(eval_state, graphs, rng)
    total_loss, (
        denoising_loss, _
    ) = loss.denoising_loss(
        preds=preds, graphs=graphs, position_noise=position_noise
    )

    # Consider only valid graphs.
    mask = jraph.get_graph_padding_mask(graphs)
    return Metrics.single_from_model_output(
        total_loss=total_loss,
        denoising_loss=denoising_loss,
        mask=mask,
    )


def evaluate_model(
    eval_state: train_state.TrainState,
    datasets: Iterator[datatypes.Fragments],
    splits: Iterable[str],
    rng: chex.PRNGKey,
    add_noise_to_positions: bool,
    noise_std: float,
) -> Dict[str, metrics.Collection]:
    """Evaluates the model on metrics over the specified splits."""

    # Loop over each split independently.
    eval_metrics = {}
    for split in splits:
        split_metrics = None

        # Loop over graphs.
        for graphs in datasets[split].as_numpy_iterator():
            graphs = datatypes.Fragments.from_graphstuple(graphs)

            # Compute metrics for this batch.
            step_rng, rng = jax.random.split(rng)
            batch_metrics = evaluate_step(eval_state, graphs, step_rng, add_noise_to_positions, noise_std)

            # Update metrics.
            if split_metrics is None:
                split_metrics = batch_metrics
            else:
                split_metrics = split_metrics.merge(batch_metrics)

        eval_metrics[split] = split_metrics

    return eval_metrics


def train_and_evaluate(
    config: ml_collections.FrozenConfigDict, workdir: str
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

    # Helper for evaluation.
    def evaluate_model_helper(
        eval_state: train_state.TrainState,
        step: int,
        rng: chex.PRNGKey,
        is_final_eval: bool,
    ) -> Dict[str, metrics.Collection]:
        # Final eval splits are usually different.
        if is_final_eval:
            splits = ["train_eval_final", "val_eval_final", "test_eval_final"]
        else:
            splits = ["train_eval", "val_eval", "test_eval"]

        # Evaluate the model.
        with report_progress.timed("eval"):
            eval_metrics = evaluate_model(
                eval_state,
                datasets,
                splits,
                rng,
                add_noise_to_positions=config.add_noise_to_positions,
                noise_std=config.position_noise_std,
            )

        # Compute and write metrics.
        for split in splits:
            eval_metrics[split] = eval_metrics[split].compute()
            writer.write_scalars(step, train.add_prefix_to_keys(eval_metrics[split], split))
        writer.flush()

        return eval_metrics

    # Create writer for logs.
    writer = metric_writers.create_default_writer(workdir)
    writer.write_hparams(config.to_dict())

    # Get datasets, organized by split.
    logging.info("Obtaining datasets.")
    rng = jax.random.PRNGKey(config.rng_seed)
    rng, dataset_rng = jax.random.split(rng)
    # datasets = input_pipeline.get_datasets(dataset_rng, config)
    datasets = input_pipeline_tf.get_datasets(dataset_rng, config)

    # Create and initialize the network.
    logging.info("Initializing network.")
    train_iter = datasets["train"].as_numpy_iterator()
    init_graphs = next(train_iter)
    net = models.create_model(config, run_in_evaluation_mode=False)

    rng, init_rng = jax.random.split(rng)
    params = jax.jit(net.init)(init_rng, init_graphs)
    parameter_overview.log_parameter_overview(params)

    # Create the optimizer.
    tx = train.create_optimizer(config)

    # Create the training state.
    state = train_state.TrainState.create(
        apply_fn=jax.jit(net.apply), params=params, tx=tx
    )

    # Create a corresponding evaluation state.
    eval_net = models.create_model(config, run_in_evaluation_mode=False)
    eval_state = state.replace(apply_fn=jax.jit(eval_net.apply))

    # Set up checkpointing of the model.
    # We will record the best model seen during training.
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    ckpt = checkpoint.Checkpoint(checkpoint_dir, max_to_keep=5)
    restored = ckpt.restore_or_initialize(
        {
            "state": state,
            "best_state": state,
            "step_for_best_state": 1.0,
            "metrics_for_best_state": None,
        }
    )
    state = restored["state"]
    best_state = restored["best_state"]
    step_for_best_state = restored["step_for_best_state"]
    metrics_for_best_state = restored["metrics_for_best_state"]
    if metrics_for_best_state is None:
        min_val_loss = float("inf")
    else:
        min_val_loss = metrics_for_best_state["val_eval"]["total_loss"]
    initial_step = int(state.step) + 1

    # Save the config for reproducibility.
    config_path = os.path.join(workdir, "config.yml")
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Hooks called periodically during training.
    report_progress = periodic_actions.ReportProgress(
        num_train_steps=config.num_train_steps, writer=writer
    )
    profile = periodic_actions.Profile(
        logdir=workdir,
        every_secs=10800,
    )
    hooks = [report_progress, profile]

    # Begin training loop.
    logging.info("Starting training.")
    train_metrics = None

    for step in range(initial_step, config.num_train_steps + 1):
        # Log, if required.
        first_or_last_step = step in [initial_step, config.num_train_steps]
        if step % config.log_every_steps == 0 or first_or_last_step:
            if train_metrics is not None:
                writer.write_scalars(
                    step, train.add_prefix_to_keys(train_metrics.compute(), "train")
                )
            train_metrics = None

        # Evaluate on validation and test splits, if required.
        if step % config.eval_every_steps == 0 or first_or_last_step:
            eval_state = eval_state.replace(params=state.params)

            # Evaluate on validation and test splits.
            rng, eval_rng = jax.random.split(rng)
            eval_metrics = evaluate_model_helper(
                eval_state,
                step,
                eval_rng,
                is_final_eval=False,
            )

            # Note best state seen so far.
            # Best state is defined as the state with the lowest validation loss.
            if eval_metrics["val_eval"]["total_loss"] < min_val_loss:
                min_val_loss = eval_metrics["val_eval"]["total_loss"]
                metrics_for_best_state = eval_metrics
                best_state = state
                step_for_best_state = step
                logging.info("New best state found at step %d.", step)

            # Save the current state and best state seen so far.
            with open(os.path.join(checkpoint_dir, f"params_{step}.pkl"), "wb") as f:
                pickle.dump(state.params, f)
            with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
                pickle.dump(best_state.params, f)
            ckpt.save(
                {
                    "state": state,
                    "best_state": best_state,
                    "step_for_best_state": step_for_best_state,
                    "metrics_for_best_state": metrics_for_best_state,
                }
            )

        # Get a batch of graphs.
        try:
            graphs = next(train_iter)
            graphs = datatypes.Fragments.from_graphstuple(graphs)

        except StopIteration:
            logging.info("No more training data. Continuing with final evaluation.")
            break

        # Perform one step of training.
        with jax.profiler.StepTraceAnnotation("train_step", step_num=step):
            step_rng, rng = jax.random.split(rng)
            state, batch_metrics = train_step(
                state,
                graphs,
                rng=step_rng,
                add_noise_to_positions=config.add_noise_to_positions,
                noise_std=config.position_noise_std,
            )

        # Update metrics.
        if train_metrics is None:
            train_metrics = batch_metrics
        else:
            train_metrics = train_metrics.merge(batch_metrics)

        # Quick indication that training is happening.
        logging.log_first_n(logging.INFO, "Finished training step %d.", 10, step)
        for hook in hooks:
            hook(step)

    # Once training is complete, return the best state and corresponding metrics.
    logging.info(
        "Evaluating best state from step %d at the end of training.",
        step_for_best_state,
    )
    eval_state = eval_state.replace(params=best_state.params)

    # Evaluate on validation and test splits, but at the end of training.
    rng, eval_rng = jax.random.split(rng)
    final_metrics_for_best_state = evaluate_model_helper(
        eval_state,
        step,
        eval_rng,
        is_final_eval=True,
    )

    # Checkpoint the best state and corresponding metrics seen during training.
    # Save pickled parameters for easy access during evaluation.
    with report_progress.timed("checkpoint"):
        with open(os.path.join(checkpoint_dir, "params_best.pkl"), "wb") as f:
            pickle.dump(best_state.params, f)
        ckpt.save(
            {
                "state": state,
                "best_state": best_state,
                "step_for_best_state": step_for_best_state,
                "metrics_for_best_state": metrics_for_best_state,
            }
        )

    return best_state, final_metrics_for_best_state