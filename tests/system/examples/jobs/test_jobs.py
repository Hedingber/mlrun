import os

import kfp.dsl
import kfp.compiler

from mlrun import (
    code_to_function,
    new_task,
    run_pipeline,
    wait_for_pipeline_completion,
    get_run_db,
)
from mlrun.platforms.other import mount_v3io

from tests.system.base import TestMLRunSystem


@TestMLRunSystem.skip_test_if_env_not_configured
class TestJobs(TestMLRunSystem):
    def custom_setup(self):
        code_path = str(self.assets_path / "jobs_function.py")

        self._logger.debug("Creating trainer job")
        self._trainer = code_to_function(
            name="my-trainer", kind="job", filename=code_path
        )

        self._trainer.spec.build.commands.append("pip install pandas")
        self._trainer.spec.build.base_image = "mlrun/mlrun"
        self._trainer.spec.command = code_path
        self._trainer.apply(mount_v3io())

        self._logger.debug("Deploying trainer")
        self._trainer.deploy(with_mlrun=False)

    def test_run_training_job(self):
        output_path = str(self.results_path / "{{run.uid}}")

        self._logger.debug("Creating base task")
        base_task = new_task(artifact_path=output_path).set_label("stage", "dev")

        # run our training task, with hyper params, and select the one with max accuracy
        self._logger.debug("Running task with hyper params")
        train_task = new_task(
            name="my-training", handler="training", params={"p1": 9}, base=base_task
        )
        train_run = self._trainer.run(train_task)

        # running validation, use the model result from the previous step
        self._logger.debug("Running validation using the model from the previous step")
        model = train_run.outputs["mymodel"]
        self._trainer.run(base_task, handler="validation", inputs={"model": model})

    def test_run_kubeflow_pipeline(self):
        @kfp.dsl.pipeline(name="job test", description="demonstrating mlrun usage")
        def job_pipeline(p1: int = 9) -> None:
            """Define our pipeline.

            :param p1: A model parameter.
            """

            train = self._trainer.as_step(
                handler="training", params={"p1": p1}, outputs=["mymodel"]
            )

            self._trainer.as_step(
                handler="validation",
                inputs={"model": train.outputs["mymodel"]},
                outputs=["validation"],
            )

        kfp.compiler.Compiler().compile(job_pipeline, "jobpipe.yaml")
        artifact_path = "v3io:///users/admin/kfp/{{workflow.uid}}/"
        arguments = {"p1": 8}
        workflow_run_id = run_pipeline(
            job_pipeline, arguments, experiment="my-job", artifact_path=artifact_path
        )

        wait_for_pipeline_completion(workflow_run_id)

        # TODO: understand why a single db instantiation isn't enough, and fix the bug in the db
        self._run_db = get_run_db()
        runs = self._run_db.list_runs(
            project=self.project_name, labels=f"workflow={workflow_run_id}"
        )
        assert len(runs) == 2

        validation_run = runs[0]
        training_run = runs[1]
        self._verify_run_metadata(
            training_run["metadata"],
            uid=training_run["metadata"]["uid"],
            name="my-trainer-training",
            project=self.project_name,
            labels={
                "v3io_user": self._test_env["V3IO_USERNAME"],
                "owner": self._test_env["V3IO_USERNAME"],
                "kind": "job",
                "category": "tests",
            },
        )
        self._verify_run_metadata(
            validation_run["metadata"],
            uid=validation_run["metadata"]["uid"],
            name="my-trainer-validation",
            project=self.project_name,
            labels={
                "v3io_user": self._test_env["V3IO_USERNAME"],
                "owner": self._test_env["V3IO_USERNAME"],
                "kind": "job",
            },
        )
        self._verify_run_spec(
            training_run["spec"],
            parameters={"p1": 8},
            outputs=["mymodel", "run_id"],
            output_path=f"v3io:///users/admin/kfp/{workflow_run_id}/",
            inputs={},
            data_stores=[],
        )
        self._verify_run_spec(
            validation_run["spec"],
            parameters={},
            outputs=["validation", "run_id"],
            output_path=f"v3io:///users/admin/kfp/{workflow_run_id}/",
            inputs={
                "model": f"store://artifacts/{self.project_name}/my-trainer-training_mymodel:{workflow_run_id}",
            },
            data_stores=[],
        )

        # remove compiled jobpipe.yaml file
        os.remove("jobpipe.yaml")
