"""Web repair loop: deploy -> screenshot -> inspect -> fix -> redeploy.

This is the module that makes "perbaiki website yang tadi" actually work, and
it is deliberately bounded: at most ``max_rounds`` iterations, each one
producing evidence (screenshots + issue list) that the user can see. If the
issues do not shrink between rounds we stop and report honestly instead of
burning tokens in a loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger
from app.services.browser_qa import BrowserQA, SiteReport
from app.services.deploy_service import DeployService, Deployment, zip_project
from app.services.web_builder import WebBuilder, WebProject

log = get_logger(__name__)


@dataclass
class RepairRound:
    index: int
    issues: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    deployed: Deployment | None = None


@dataclass
class RepairResult:
    ok: bool
    project: WebProject
    rounds: list[RepairRound] = field(default_factory=list)
    deployment: Deployment | None = None
    bundle: str = ""
    final_report: SiteReport | None = None

    def summary(self) -> str:
        if self.ok:
            rounds = len(self.rounds)
            return f"Website lolos QA setelah {rounds} putaran perbaikan."
        remaining = self.final_report.issues() if self.final_report else []
        return "Belum lolos QA. Sisa masalah:\n" + "\n".join(f"- {i}" for i in remaining[:6])


class WebRepairService:
    def __init__(
        self,
        settings,
        *,
        builder: WebBuilder,
        deployer: DeployService,
        qa: BrowserQA,
        max_rounds: int = 3,
    ):
        self.settings = settings
        self.builder = builder
        self.deployer = deployer
        self.qa = qa
        self.max_rounds = max_rounds

    async def ship(
        self,
        project: WebProject,
        *,
        project_name: str,
        env_vars: dict[str, str] | None = None,
        progress=None,
        fill_forms: bool = False,
    ) -> RepairResult:
        """Deploy, QA, repair, redeploy until QA passes or rounds run out."""
        result = RepairResult(ok=False, project=project)
        previous_issue_count = 10**6

        for round_index in range(1, self.max_rounds + 1):
            if progress:
                await progress(
                    20 + int(60 * (round_index - 1) / self.max_rounds),
                    f"Putaran {round_index}/{self.max_rounds}: deploy",
                )

            deployment = await self._deploy(project, project_name, env_vars)
            if deployment is None:
                # No deploy credentials: QA the local build instead of failing.
                if progress:
                    await progress(50, "Tidak ada kredensial deploy, QA lokal")
                report = await self.qa.check_local_file(
                    project.entry, fill_forms=fill_forms
                )
                report_url = f"file://{project.entry}"
            else:
                if progress:
                    await progress(50, f"QA otomatis: {deployment.url}")
                report = await self.qa.check_site(deployment.url, fill_forms=fill_forms)
                report_url = deployment.url
                result.deployment = deployment

            round_entry = RepairRound(
                index=round_index,
                issues=report.issues(),
                screenshots=report.screenshots(),
                deployed=deployment,
            )
            result.rounds.append(round_entry)
            result.final_report = report

            if report.ok:
                result.ok = True
                if deployment is None:
                    result.bundle = str(zip_project(project.root))
                if progress:
                    await progress(100, "QA lolos")
                return result

            # Stop if we are not making progress: repairing the same issues again
            # just burns tokens.
            if len(report.issues()) >= previous_issue_count and round_index > 1:
                log.info("repair stalled at %s issues; stopping", len(report.issues()))
                if progress:
                    await progress(80, "Perbaikan tidak menurunkan jumlah masalah, berhenti")
                break
            previous_issue_count = len(report.issues())

            if progress:
                await progress(
                    60 + int(30 * round_index / self.max_rounds),
                    f"Perbaiki {len(report.issues())} masalah",
                )
            try:
                project = await self.builder.repair(project, report.issues(), progress=progress)
            except Exception as exc:  # noqa: BLE001
                log.warning("repair round failed: %s", exc)
                break

        if result.deployment is None:
            result.bundle = str(zip_project(project.root))
        return result

    async def _deploy(
        self, project: WebProject, project_name: str, env_vars: dict[str, str] | None
    ) -> Deployment | None:
        try:
            if project.kind == "static":
                return await self.deployer.deploy_static(project.root, project_name=project_name)
            return await self.deployer.deploy_fullstack(
                project.root, project_name=project_name, env_vars=env_vars or {}
            )
        except Exception as exc:  # noqa: BLE001
            log.info("deploy unavailable (%s); falling back to local QA", exc)
            return None

    async def repair_existing(
        self,
        url: str,
        *,
        project: WebProject,
        project_name: str,
        progress=None,
    ) -> RepairResult:
        """Repair an already deployed site: QA the live URL, fix, redeploy."""
        result = RepairResult(ok=False, project=project)
        if progress:
            await progress(10, f"Buka {url} dan cek masalahnya")
        report = await self.qa.check_site(url, fill_forms=True)
        result.final_report = report
        result.rounds.append(
            RepairRound(index=0, issues=report.issues(), screenshots=report.screenshots())
        )
        if report.ok:
            result.ok = True
            return result
        return await self.ship(project, project_name=project_name, progress=progress)
