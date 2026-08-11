"use client";

import { useCallback, useMemo, useState, type CSSProperties, type ReactNode } from "react";

import {
  CaretIcon,
  FileIcon,
  FolderIcon,
  NewFolderIcon,
  SearchIcon,
  ShieldIcon,
  UploadIcon,
} from "@/components/studio/icons";
import { LibrarySection, type LibraryState } from "@/components/studio/LibrarySection";
import { docGlyph } from "@/lib/studio-marks";
import { countLibraryDocs, type LibraryDoc } from "@/lib/studio-library";
import type { StudioDoc, TreeNode } from "@/lib/studio-types";

/** The glyph is a colour and a shape; screen readers get the same state in words. */
const GLYPH_LABEL: Record<ReturnType<typeof docGlyph>, string> = {
  clean: "checked, no open findings",
  findings: "has open findings",
  unchecked: "not checked",
  checking: "checking now",
  // Every finding has a recorded judgement, but not every one was fixed, so this
  // is deliberately not the same state as clean.
  settled: "every finding recorded",
};

/** Neither action has a document service behind it yet, so both say so on hover. */
const PENDING_TITLE = "Uploading arrives with the document service.";

/** A CTD coordinate or an SOP number: uppercase and digits with dots and dashes
 * between them. Deliberately narrow -- a word that merely opens a filename is
 * not a code, and a wrong split is worse than no split. */
const DOC_CODE = /^[0-9A-Z][0-9A-Z.-]*[0-9A-Z]$/;

/**
 * Split a filename into its leading identifier and the rest. "3.2.S.4.1
 * Specification.docx" is a coordinate plus a name and the two carry different
 * weight in the rail; a filename with no code in front stays a single line.
 * Pure and exported so the derivation is testable without a DOM.
 */
export function splitDocName(name: string): { code: string | null; title: string } {
  const cut = name.search(/\s/);
  if (cut > 0) {
    const head = name.slice(0, cut);
    if (DOC_CODE.test(head)) return { code: head, title: name.slice(cut + 1).trim() };
  }
  return { code: null, title: name };
}

function countDocs(nodes: TreeNode[]): number {
  return nodes.reduce((total, node) => total + (node.kind === "doc" ? 1 : countDocs(node.children)), 0);
}

/**
 * Keep documents whose name matches, and keep a folder only when something under
 * it survived. Filtering the tree rather than flattening it preserves the path a
 * document lives at, which is how an analyst recognises the right 3.2.P.5.1.
 */
function filterTree(nodes: TreeNode[], docs: Record<string, StudioDoc>, query: string): TreeNode[] {
  const out: TreeNode[] = [];
  for (const node of nodes) {
    if (node.kind === "doc") {
      const doc = docs[node.docId];
      if (doc && doc.name.toLowerCase().includes(query)) out.push(node);
      continue;
    }
    const children = filterTree(node.children, docs, query);
    if (children.length > 0) out.push({ ...node, children });
  }
  return out;
}

/** Rows nest by indent, not by containment, so depth has to be carried in. */
function indent(depth: number): CSSProperties {
  return { paddingLeft: `${0.4 + depth * 0.8}rem` };
}

interface RepositoryTreeProps {
  tree: TreeNode[];
  docs: Record<string, StudioDoc>;
  /** Active working-document id, or null while a library doc is on the canvas. */
  activeId: string | null;
  library: LibraryState;
  /** Active library doc id ("psg-.."), or null while a draft is on the canvas. */
  activeLibraryId: string | null;
  open: boolean;
  checking: boolean;
  onOpenDoc: (id: string) => void;
  onOpenLibraryDoc: (doc: LibraryDoc) => void;
  onRetryLibrary: () => void;
  onCheck: () => void;
  /** Runs every working document. It belongs with the repository it acts on. */
  onRunFullCheck: () => void;
}

export function RepositoryTree({
  tree,
  docs,
  activeId,
  library,
  activeLibraryId,
  open,
  checking,
  onOpenDoc,
  onOpenLibraryDoc,
  onRetryLibrary,
  onCheck,
  onRunFullCheck,
}: RepositoryTreeProps) {
  const [query, setQuery] = useState("");
  // Folders start expanded, so the state that has to be tracked is the ones the
  // analyst closed. An empty set is the default view.
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set<string>());

  const needle = query.trim().toLowerCase();
  const searching = needle.length > 0;

  const docCount = useMemo(() => countDocs(tree), [tree]);
  const visible = useMemo(
    () => (searching ? filterTree(tree, docs, needle) : tree),
    [docs, needle, searching, tree],
  );

  const toggleFolder = useCallback((id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  function renderNodes(nodes: TreeNode[], depth: number): ReactNode {
    return nodes.map((node) => {
      if (node.kind === "doc") {
        const doc = docs[node.docId];
        if (!doc) return null;
        const isActive = doc.id === activeId;
        const glyph = docGlyph(doc);
        const { code, title } = splitDocName(doc.name);
        return (
          <div key={node.id}>
            <button
              type="button"
              className={`st-node st-node--doc${isActive ? " is-active" : ""}`}
              style={indent(depth)}
              aria-current={isActive ? "true" : undefined}
              onClick={() => onOpenDoc(doc.id)}
            >
              <FileIcon className="st-node__icon" />
              {/* The two lines are typography. The filename reaches the
                  accessible name whole in the span below, so it is never
                  announced in pieces and never depends on how these lay out. */}
              <span className="st-node__name" aria-hidden="true">
                {code ? <span className="st-node__code">{code}</span> : null}
                <span className="st-node__title">{title}</span>
              </span>
              <span className="studio__sr">{doc.name}</span>
              <span className={`st-glyph st-glyph--${glyph}`} />
              <span className="studio__sr">{GLYPH_LABEL[glyph]}</span>
            </button>
          </div>
        );
      }

      // A search result is useless behind a closed folder, so a live query opens
      // every surviving branch without disturbing what the analyst collapsed.
      const isOpen = searching || !collapsed.has(node.id);
      const groupId = `st-folder-${node.id}`;
      return (
        <div key={node.id}>
          <button
            type="button"
            className="st-node st-node--folder"
            style={indent(depth)}
            aria-expanded={isOpen}
            aria-controls={groupId}
            onClick={() => toggleFolder(node.id)}
          >
            <CaretIcon className={`st-node__caret${isOpen ? " st-node__caret--open" : ""}`} />
            <FolderIcon className="st-node__icon" />
            <span className="st-node__label">{node.label}</span>
            {node.badge ? <span className="st-chip st-node__badge">{node.badge}</span> : null}
          </button>
          <div id={groupId} hidden={!isOpen}>
            {renderNodes(node.children, depth + 1)}
          </div>
        </div>
      );
    });
  }

  return (
    <aside className={`st-tree${open ? " is-open" : ""}`} aria-label="Document repository">
      <div className="st-tree__head">
        <span className="st-eyebrow">Repository</span>
        <span className="st-chip">
          {docCount} {docCount === 1 ? "doc" : "docs"}
        </span>
        <div className="st-tree__actions">
          <button type="button" className="st-icon-btn" aria-label="New folder" title={PENDING_TITLE} disabled>
            <NewFolderIcon />
          </button>
          <button type="button" className="st-icon-btn" aria-label="Upload document" title={PENDING_TITLE} disabled>
            <UploadIcon />
          </button>
        </div>
      </div>

      <div className="st-field st-search">
        <SearchIcon />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search documents"
          aria-label="Search documents"
          autoComplete="off"
          spellCheck={false}
        />
      </div>

      <div className="st-tree__scroll">
        <h3 className="st-eyebrow st-tree__section">Working documents</h3>
        {visible.length > 0 ? (
          renderNodes(visible, 0)
        ) : (
          <div className="st-tree__empty">
            {searching ? "No documents match that search." : "No documents in this repository yet."}
          </div>
        )}

        <h3 className="st-eyebrow st-tree__section">
          Reference library
          {library.phase === "ready"
            ? ` - ${countLibraryDocs(library.buckets)} ${
                countLibraryDocs(library.buckets) === 1 ? "PSG" : "PSGs"
              }`
            : ""}
        </h3>
        <LibrarySection
          state={library}
          needle={needle}
          activeLibraryId={activeLibraryId}
          onOpen={onOpenLibraryDoc}
          onRetry={onRetryLibrary}
        />
      </div>

      <div className="st-check">
        <button
          type="button"
          className="st-btn st-btn--primary st-btn--lg st-btn--block"
          onClick={onCheck}
          // Disabled, not hidden, while a reference PSG is open: the footer is
          // a stable landmark, and the swapped note explains the refusal.
          disabled={checking || activeLibraryId !== null}
        >
          <ShieldIcon />
          {checking ? "Checking..." : "Check this document"}
        </button>
        <button
          type="button"
          className="st-btn st-btn--outline st-btn--block"
          onClick={onRunFullCheck}
          disabled={checking || activeLibraryId !== null}
        >
          Check all {docCount} {docCount === 1 ? "document" : "documents"}
        </button>
        <p className="st-check__note">
          {activeLibraryId !== null
            ? "Reference PSGs are FDA source documents. Compliance checks run on working documents."
            : "Against ICH, USP, 21 CFR and your internal SOPs."}
        </p>
      </div>
    </aside>
  );
}
