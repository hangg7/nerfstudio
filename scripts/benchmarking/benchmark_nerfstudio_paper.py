"""
Benchmarking script for nerfstudio paper.

- nerfacto and instant-ngp methods on mipnerf360 data
- nerfacto ablations
"""

import threading
import time
from pathlib import Path

import GPUtil

from nerfstudio.utils.scripts import run_command

mipnerf360_capture_names = [
    "bicycle",
    "garden",
    "stump",
    "room",
    "counter",
    "kitchen",
    "bonsai",
]  # 7 splits
# mipnerf360_capture_names = ["bicycle", "bonsai"]  # 7 splits
# 1/8 of input images used in the paper = 0.125 -> 1 - this = 0.875
mipnerf360_table_rows = [
    # nerfacto method
    (
        "nerfacto-w/o-pose-app",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance False --pipeline.datamanager.camera-optimizer.mode off --pipeline.model.use-appearance-embedding False nerfstudio-data --downscale-factor 4 --train-split-percentage 0.875",
    ),
    # instant-ngp method
    # (
    #     "instant-ngp-w/o-pose-app",
    #     "instant-ngp",
    #     "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance False --pipeline.datamanager.camera-optimizer.mode off --pipeline.model.use-appearance-embedding False nerfstudio-data --downscale-factor 4 --train-split-percentage 0.875",
    # ),
]


ablations_capture_names = [
    "Egypt",
    "person",
    "kitchen",
    "plane",
    "dozer",
    "floating-tree",
    "aspen",
    "stump",
    "sculpture",
    "Giannini-Hall",
]

ablations_table_rows = [
    (
        "nerfacto",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True",
    ),
    (
        "w/o-pose",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.datamanager.camera-optimizer.mode off",
    ),
    (
        "w/o-app",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance False --pipeline.model.use-appearance-embedding False",
    ),
    (
        "w/o-pose-app",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance False --pipeline.datamanager.camera-optimizer.mode off --pipeline.model.use-appearance-embedding False",
    ),
    (
        "1-prop-network",
        "nerfacto",
        '--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.model.num-proposal-samples-per-ray "256" --pipeline.model.num_proposal_iterations 1',
    ),
    (
        "l2-contraction",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.model.scene-contraction-norm l2",
    ),
    (
        "shared-prop-network",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.model.use-same-proposal-network True",
    ),
    (
        "random-background-color",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.model.background-color random",
    ),
    (
        "no-contraction",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance True --pipeline.model.use-bounded True --pipeline.model.use-scene-contraction False nerfstudio-data --scale_factor 0.125",
    ),
    # experiment with synthetic dataset settings
    (
        "synthetic-on-real",
        "nerfacto",
        "--pipeline.eval_optimize_cameras True --pipeline.eval_optimize_appearance False --pipeline.datamanager.camera-optimizer.mode off --pipeline.model.use-appearance-embedding False --pipeline.model.use-bounded True --pipeline.model.use-scene-contraction False nerfstudio-data --scale_factor 0.125",
    ),
]


def main(capture_names, table_rows, data_path: Path = Path("data/nerfstudio")):
    """Main method."""
    # 30K iterations

    # make a list of all the jobs that need to be fun
    jobs = []
    for capture_name in capture_names:
        for table_row_name, method, table_row_command in table_rows:
            command = " ".join(
                (
                    f"ns-train {method}",
                    "--vis wandb",
                    f"--data { data_path / capture_name}",
                    "--output-dir outputs/nerfacto-ablations",
                    "--trainer.steps-per-eval-batch 0 --trainer.steps-per-eval-image 0",
                    "--trainer.steps-per-eval-all-images 5000 --trainer.max-num-iterations 300001",
                    f"--wandb-name {capture_name}_{table_row_name}",
                    f"--experiment-name {capture_name}_{table_row_name}",
                    table_row_command,
                )
            )
            jobs.append(command)

    while jobs:

        # check which GPUs have capacity to run these jobs
        """Returns the available GPUs."""
        gpu_devices_available = GPUtil.getAvailable(
            order="first", limit=10, maxMemory=0.1
        )

        print("Available GPUs: ", gpu_devices_available)

        # thread list
        threads = []
        while gpu_devices_available and jobs:
            gpu = gpu_devices_available.pop(0)
            command = f"CUDA_VISIBLE_DEVICES={gpu} " + jobs.pop(0)

            def task():
                print("Starting command: ", command)
                out = run_command(command, verbose=False)
                # time.sleep(5)
                print("Finished command: ", command)

            threads.append(threading.Thread(target=task))
            threads[-1].start()

            # NOTE(ethan): need a delay otherwise the wandb/tensorboard naming is messed up
            # not sure why?
            time.sleep(5)

        # wait for all threads to finish
        for t in threads:
            t.join()

        print("Finished all threads")


if __name__ == "__main__":
    # pass
    main(
        mipnerf360_capture_names,
        mipnerf360_table_rows,
        data_path=Path("data/nerfstudio-data-mipnerf360"),
    )
    # main(ablations_capture_names, ablations_table_rows)
