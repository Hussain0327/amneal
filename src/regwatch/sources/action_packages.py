"""Drugs@FDA review and action-package document handler."""

from __future__ import annotations

import httpx

from regwatch.sources._utils import bare_application_number
from regwatch.sources.drugsfda import DrugsFdaHandler, get_drugsfda_snapshot
from regwatch.sources.policy import FdaSourceFamily
from regwatch.sources.types import SourceKind, SourceQuery, SourceRecord


class ActionPackageHandler:
    source = SourceKind.ACTION_PACKAGE

    def search(
        self,
        query: SourceQuery,
        *,
        client: httpx.Client | None = None,
    ) -> list[SourceRecord]:
        snapshot = get_drugsfda_snapshot(client=client)
        applications = DrugsFdaHandler().search(query, client=client)
        out: list[SourceRecord] = []
        for application in applications:
            appl_no = bare_application_number(application.identifiers.get("application_number"))
            for document in snapshot.application_documents(appl_no):
                if document.source_family is not FdaSourceFamily.ACTION_PACKAGE:
                    continue
                out.append(
                    SourceRecord(
                        source=self.source,
                        title=document.title,
                        source_url=document.source_url,
                        identifiers={
                            "application_number": document.application_number,
                            "application_docs_id": document.application_docs_id,
                        },
                        fields={
                            "document_type": document.document_type.value,
                            "document_date": document.document_date,
                            "submission_type": document.submission_type,
                            "submission_number": document.submission_number,
                        },
                    )
                )
                if len(out) >= query.limit:
                    return out
        return out
