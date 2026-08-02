"""Train the SEVPS estimators and report honestly on whether to deploy them.

    python manage.py train_models                    # all, from real history
    python manage.py train_models --synthetic        # bootstrap the pipeline
    python manage.py train_models congestion --save

Nothing is written to disk unless ``--save`` is given *and* the model beat its
statistical baseline. A model that ties its baseline adds a dependency, a
failure mode and an explanation surface for no accuracy - so the default is to
report and discard.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.brain.ml import estimators as est
from apps.brain.ml import training

TRAINABLE = ("congestion", "eta_residual", "corridor_success", "emergency_priority")


class Command(BaseCommand):
    help = "Train SEVPS ML estimators; deploy only those that beat their baseline."

    def add_arguments(self, parser):
        parser.add_argument("estimators", nargs="*", help=f"Any of: {', '.join(TRAINABLE)}")
        parser.add_argument("--synthetic", action="store_true",
                            help="Generate labelled data instead of using history.")
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--min-samples", type=int, default=200)
        parser.add_argument("--save", action="store_true",
                            help="Persist models that beat their baseline.")
        parser.add_argument("--force", action="store_true",
                            help="Persist even a model that lost. Rarely correct.")

    def handle(self, *args, **options):
        try:
            import sklearn  # noqa: F401
        except ImportError as exc:
            raise CommandError(
                "scikit-learn is required:\n"
                "    pip install scikit-learn numpy joblib shap"
            ) from exc

        names = options["estimators"] or list(TRAINABLE)
        for name in names:
            if name not in TRAINABLE:
                raise CommandError(f"{name!r} is not trainable. Choose from: {', '.join(TRAINABLE)}")

        if options["synthetic"]:
            self.stdout.write(self.style.WARNING(
                "Training on SYNTHETIC data. This exercises the pipeline end to end;\n"
                "it says nothing about real-world accuracy. Models are tagged accordingly.\n"
            ))

        self.stdout.write(self.style.MIGRATE_HEADING("Training\n"))
        results = []
        for name in names:
            try:
                outcome = training.train(
                    name,
                    synthetic=options["synthetic"],
                    days=options["days"],
                    min_samples=options["min_samples"],
                )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  {name:20} failed: {exc}"))
                continue

            if outcome is None:
                self.stdout.write(
                    f"  {name:20} skipped - not enough history "
                    f"(need {options['min_samples']} samples; try --synthetic)"
                )
                continue

            model, result = outcome
            self.stdout.write(result.report())
            results.append((model, result))

            if options["save"] and (result.should_deploy or options["force"]):
                estimator = est.get(name)
                path = estimator.save(model, result.metadata)
                note = "" if result.should_deploy else "  (forced despite losing)"
                self.stdout.write(self.style.SUCCESS(f"      saved -> {path}{note}"))

        self._summary(results, options)

    def _summary(self, results, options):
        if not results:
            self.stdout.write("\nNothing trained.")
            return

        deployable = [r for _, r in results if r.should_deploy]
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(deployable)}/{len(results)} model(s) beat their baseline."
            )
        )
        if not options["save"] and deployable:
            self.stdout.write("Re-run with --save to deploy them.")

        rejected = [r for _, r in results if not r.should_deploy]
        for result in rejected:
            self.stdout.write(
                self.style.WARNING(
                    f"  {result.estimator}: keeping the statistical baseline - the trained "
                    f"model was only {result.improvement:+.1%} different, which does not "
                    f"justify the added dependency."
                )
            )

        if any(r.synthetic for _, r in results) and options["save"]:
            self.stdout.write(self.style.WARNING(
                "\nSynthetic models are saved for pipeline testing only. Retrain on real\n"
                "history before relying on any prediction they produce."
            ))
