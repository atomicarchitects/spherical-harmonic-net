"""Creates plots to analyze trained models."""

from typing import Sequence, Dict

from absl import app
from absl import flags
from absl import logging
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from schnetpack import Properties
import seaborn as sns

import sys

sys.path.append("..")

import analyses.analysis as analysis


ALL_METRICS = ["total_loss", "position_loss", "focus_loss", "atom_type_loss"]
ALL_MODELS = ["mace", "e3schnet", "nequip"]

FLAGS = flags.FLAGS


def get_title_for_metric(metric: str) -> str:
    """Returns the title for the given metric."""
    return " ".join(metric.split("_")).title()


def get_title_for_model(model: str) -> str:
    """Returns the title for the given model."""
    if model == "e3schnet":
        return "E3SchNet"
    elif model == "mace":
        return "MACE"
    elif model == "nequip":
        return "NequIP"
    return model.title()


def get_title_for_split(split):
    """Returns the title for the given split."""
    if split == "train_eval_final":
        return "Train"
    elif split == "val_eval_final":
        return "Validation"
    if split == "test_eval_final":
        return "Test"
    return split.title()


def plot_performance_for_parameters(
    metrics: Sequence[str], results: Dict[str, pd.DataFrame], outputdir: str
) -> None:
    """Creates a line plot for each metric as a function of number of parameters."""

    def plot_metric(model: str, metric: str, split: str):
        # Set style.
        sns.set_theme(style="darkgrid")

        # Get all values of num_interactions in this split.
        split_num_interactions = (
            results[split]["num_interactions"].drop_duplicates().sort_values().values
        )

        # One figure for each value of num_interactions.
        fig, axs = plt.subplots(
            ncols=len(split_num_interactions),
            figsize=(len(split_num_interactions) * 4, 6),
            sharey=True,
            squeeze=True,
        )
        try:
            len(axs)
        except TypeError:
            axs = [axs]

        fig.suptitle(
            get_title_for_model(model) + " on " + get_title_for_split(split) + " Set"
        )

        for ax, num_interactions in zip(axs, split_num_interactions):
            # Choose the subset of data based on the number of interactions and model.
            df = results[split][results[split]["model"] == model]
            df_subset = df[df["num_interactions"] == num_interactions]

            # Skip empty dataframes.
            if not len(df_subset):
                print(
                    f"Skipping model {model}, split {split}, num_interactions {num_interactions}"
                )
                continue

            # Lineplot.
            sns.lineplot(
                data=df_subset,
                x="num_params",
                y=metric,
                hue="max_l",
                style="max_l",
                markersize=10,
                markers=True,
                dashes=True,
                ax=ax,
            )

            # Customizing different axes.
            if num_interactions == split_num_interactions[-1]:
                ax.legend(
                    title="Max L",
                    loc="center left",
                    bbox_to_anchor=(1.04, 0.5),
                    borderaxespad=0,
                    fancybox=True,
                    shadow=False,
                )
                ax.set_ylabel("")
            else:
                ax.legend().remove()
                ax.set_ylabel(get_title_for_metric(metric))

            # Axes limits.
            min_y = results[split][metric].min()
            max_y = results[split][metric].max()
            ax.set_ylim(min_y - 0.2, max_y + 0.2)

            # Labels and titles.
            ax.set_title(f"{num_interactions} Interactions")
            ax.set_xlabel("Number of Parameters")
            ax.ticklabel_format(axis="x", style="sci", scilimits=(0, 0))

        # Save figure.
        os.makedirs(os.path.join(outputdir, "num_params"), exist_ok=True)
        outputfile = os.path.join(
            outputdir, "num_params", f"{model}_{split}_{metric}_params.png"
        )
        plt.savefig(outputfile, bbox_inches="tight")
        plt.close()

    models = results["val_eval_final"]["model"].drop_duplicates().sort_values().values
    for model in models:
        for split in results:
            for metric in metrics:
                plot_metric(model, metric, split)


def plot_performance_for_max_ell(
    metrics: Sequence[str], results: Dict[str, pd.DataFrame], outputdir: str
) -> None:
    """Creates a scatter plot for each metric, grouped by max_ell."""

    def plot_metric(model: str, metric: str, split: str):
        # Set style.
        sns.set_theme(style="darkgrid")

        # Get all values of num_interactions in this split.
        split_num_interactions = (
            results[split]["num_interactions"].drop_duplicates().sort_values().values
        )

        # One figure for each value of num_interactions.
        fig, axs = plt.subplots(
            ncols=len(split_num_interactions),
            figsize=(len(split_num_interactions) * 4, 6),
            sharey=True,
            squeeze=True,
        )
        try:
            len(axs)
        except TypeError:
            axs = [axs]

        fig.suptitle(
            get_title_for_model(model) + " on " + get_title_for_split(split) + " Set"
        )

        for ax, num_interactions in zip(axs, split_num_interactions):
            # Choose the subset of data based on the number of interactions.
            df = results[split][results[split]["model"] == model]
            df_subset = df[df["num_interactions"] == num_interactions]

            # Skip empty dataframes.
            if not len(df_subset):
                logging.info(
                    f"Skipping model {model}, split {split}, num_interactions {num_interactions}"
                )
                continue

            # Scatterplot.
            ax = sns.scatterplot(
                data=df_subset,
                x="max_l",
                y=metric,
                hue="num_channels",
                size="num_channels",
                sizes=(100, 200),
                ax=ax,
            )

            # Customizing different axes.
            if num_interactions == split_num_interactions[-1]:
                ax.legend(
                    title="Number of Channels",
                    loc="center left",
                    bbox_to_anchor=(1.04, 0.5),
                    borderaxespad=0,
                    fancybox=True,
                    shadow=False,
                )
                ax.set_ylabel("")
            else:
                ax.legend().remove()
                ax.set_ylabel(get_title_for_metric(metric))

            # Axes limits.
            min_y = results[split][metric].min()
            max_y = results[split][metric].max()
            ax.set_ylim(min_y - 0.2, max_y + 0.2)

            # Labels and titles.
            ax.set_title(f"{num_interactions} Interactions")
            ax.set_xlabel("Max L")
            ax.set_xticks(np.arange(df["max_l"].min(), df["max_l"].max() + 1))

            # Add jitter to the points.
            np.random.seed(0)
            dots = ax.collections[0]
            offsets = dots.get_offsets()
            jittered_offsets = np.stack(
                [
                    offsets[:, 0]
                    + np.random.uniform(-0.1, 0.1, size=offsets[:, 0].shape),
                    offsets[:, 1],
                ],
                axis=1,
            )
            dots.set_offsets(jittered_offsets)

        # Save plot.
        os.makedirs(os.path.join(outputdir, "max_ell"), exist_ok=True)
        outputfile = os.path.join(
            outputdir, "max_ell", f"{model}_{split}_{metric}_max_ell.png"
        )
        plt.savefig(outputfile, bbox_inches="tight")
        plt.close()

    models = results["val_eval_final"]["model"].drop_duplicates().sort_values().values
    for model in models:
        for split in results:
            for metric in metrics:
                plot_metric(model, metric, split)


def plot_sample_complexity_curves(
    metrics: Sequence[str], results: Dict[str, pd.DataFrame], outputdir: str
) -> None:
    """Creates a line plot for each metric as a function of number of parameters."""

    def plot_metric(model: str, metric: str, split: str):
        # Set style.
        sns.set_theme(style="darkgrid")

        # One figure for each value of num_interactions.
        fig, ax = plt.subplots(
            ncols=1,
            figsize=(4, 6),
            sharey=True,
        )
        fig.suptitle(
            f"{get_title_for_model(model)}: Sample Complexity Curve for {get_title_for_metric(metric)} on {get_title_for_split(split)} Set"
        )

        # Use log-log scale.
        ax.set_xscale("log")
        # ax.set_yscale("log")

        # Extract results for this model.
        df_subset = results[split][results[split]["model"] == model]

        # Lineplot.
        sns.lineplot(
            data=df_subset,
            x="num_train_molecules",
            y=metric,
            hue="max_l",
            style="max_l",
            markersize=10,
            markers=True,
            dashes=True,
            ax=ax,
        )

        # Customizing different axes.
        ax.legend(
            title="Max L",
            loc="center left",
            bbox_to_anchor=(1.04, 0.5),
            borderaxespad=0,
            fancybox=True,
            shadow=False,
        )

        # Axes limits.
        min_y = results[split][metric].min()
        max_y = results[split][metric].max()
        # ax.set_ylim(max(1e-2, min_y - 0.2), max_y + 0.2)
        # ax.set_yticks(np.arange(1, 10))
        # ax.set_yticklabels(np.arange(1, 10))
        # ax.set_xticks([2000, 4000, 8000, 16000, 32000, 64000])
        # ax.set_xticklabels([2000, 4000, 8000, 16000, 32000, 64000])

        # Labels and titles.
        ax.set_ylabel(get_title_for_metric(metric))
        ax.set_xlabel("Number of Training Molecules")

        # Save figure.
        os.makedirs(outputdir, exist_ok=True)
        outputfile = os.path.join(
            outputdir, f"{model}_{split}_{metric}_sample_complexity.png"
        )
        plt.savefig(outputfile, bbox_inches="tight")
        plt.close()

    models = results["val_eval_final"]["model"].drop_duplicates().sort_values().values
    for model in models:
        for split in results:
            for metric in metrics:
                plot_metric(model, metric, split)


def plot_atom_type_hist(
        mol_path: str, outputdir: str, model: str
):
    """Creates a histogram of atom types for a given set of generated molecules."""
    mol_dict = analysis.get_mol_dict(mol_path)

    atom_type_list = np.array([])
    for n_atoms in mol_dict:
        for atoms in mol_dict[n_atoms][Properties.Z]:
            atom_type_list = np.concatenate([atom_type_list, atoms])

    atom_type_counts = {'H': 0, 'C': 0, 'N': 0, 'O': 0, 'F': 0}
    element_numbers = {1: 'H', 6: 'C', 7: 'N', 8: 'O', 9: 'F'}
    for atom_type in atom_type_list:
        atom_type_counts[element_numbers[atom_type]] += 1

    plt.bar(atom_type_counts.keys(), atom_type_counts.values())
    # TODO make `model` auto-detected from path
    plt.title(
        f"{get_title_for_model(model)}: Distribution of Atom Types in Generated Molecules"
    )

    # Save figure.
    os.makedirs(outputdir, exist_ok=True)
    outputfile = os.path.join(
        outputdir, f"{model}_atom_types.png"
    )
    plt.savefig(outputfile, bbox_inches="tight")
    plt.close()


def main(argv):
    if len(argv) > 1:
        raise app.UsageError("Too many command-line arguments.")

    # Get results.
    basedir = os.path.abspath(FLAGS.basedir)
    results = analysis.get_results_as_dataframe(ALL_MODELS, ALL_METRICS, basedir)
    logging.info(results)

    # Make plots.
    if os.path.basename(basedir).startswith("v"):
        # Extract version from basedir.
        version = os.path.basename(basedir)
        outputdir = os.path.join(os.path.abspath(FLAGS.outputdir), "plots", version)

        plot_performance_for_max_ell(ALL_METRICS, results, outputdir)
        plot_performance_for_parameters(ALL_METRICS, results, outputdir)

    if os.path.basename(basedir) == "sample_complexity":
        outputdir = os.path.join(
            os.path.abspath(FLAGS.outputdir), "plots", "extras", "sample_complexity"
        )

        plot_sample_complexity_curves(ALL_METRICS, results, outputdir)


if __name__ == "__main__":
    flags.DEFINE_string("basedir", None, "Directory where all workdirs are stored.")
    flags.DEFINE_string(
        "outputdir",
        os.path.join(os.getcwd(), "analyses"),
        "Directory where plots should be saved.",
    )

    flags.mark_flags_as_required(["basedir"])
    app.run(main)
