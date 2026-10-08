"""Modelo e ModelAdmin de exemplo (somente leitura) para ``audit.pipeline_runs``.

Copie para um app do seu projeto Django e registre no Admin. O esquema
continua sendo criado **somente** pelas migrações SQL de ``sql/``: este modelo
é ``managed = False`` — não gera migrations e não duplica o banco no ORM.

O painel mostra: run_id, operação (job_name), início, fim, status, registros
processados, erro, versão do pipeline/dataset e a última execução bem-sucedida
(exibida como aviso no topo da lista). "Versão do pipeline" vem de
``metadata->>'version_tag'``; o esquema de auditoria não tem coluna própria de
versão — limitação documentada.
"""

from django.contrib import admin, messages
from django.db import models

from src.integrations.django import latest_successful_run


class PipelineRun(models.Model):
    """Execução registrada em ``audit.pipeline_runs`` (somente leitura)."""

    run_id = models.UUIDField(primary_key=True)
    job_name = models.CharField(max_length=60, verbose_name="operação")
    started_at = models.DateTimeField(verbose_name="início")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="fim")
    status = models.CharField(max_length=20)
    records_ingested = models.IntegerField(null=True, blank=True)
    records_features = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True, verbose_name="erro")
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "audit.pipeline_runs"
        ordering = ["-started_at"]
        verbose_name = "execução do pipeline"
        verbose_name_plural = "execuções do pipeline"

    @property
    @admin.display(description="versão do pipeline")
    def versao_do_pipeline(self):
        return (self.metadata or {}).get("version_tag") or "—"

    @property
    @admin.display(description="registros processados")
    def registros_processados(self):
        return f"{self.records_ingested or 0} ingeridos / {self.records_features or 0} features"

    def __str__(self):
        return f"{self.job_name} — {self.run_id} ({self.status})"


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = (
        "run_id", "job_name", "started_at", "finished_at", "status",
        "registros_processados", "versao_do_pipeline",
    )
    list_filter = ("status", "job_name")
    search_fields = ("run_id", "job_name")
    readonly_fields = (
        "run_id", "job_name", "started_at", "finished_at", "status",
        "records_ingested", "records_features", "error_message", "metadata",
        "registros_processados", "versao_do_pipeline",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        ultima = latest_successful_run()
        if ultima:
            messages.info(
                request,
                "Última execução bem-sucedida: {} em {} (run_id {}).".format(
                    ultima["job_name"], ultima["finished_at"], ultima["run_id"]),
            )
        return super().changelist_view(request, extra_context=extra_context)
