'use client';

import { useState } from 'react';
import CIcon from '@coreui/icons-react';
import {
  cilFolder,
  cilFolderOpen,
  cilLightbulb,
  cilCaretRight,
  cilCaretBottom,
  cilCheckAlt,
} from '@coreui/icons';
import type { Idea } from './IdeasView';

export interface IdeaNode extends Idea {
  children: IdeaNode[];
}

interface IdeaTreeProps {
  nodes: IdeaNode[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  // Move `draggedId` under `targetId` (null = top-level). Optional so the
  // tree still renders read-only if a caller omits it.
  onReparent?: (draggedId: number, targetId: number | null) => void;
  // Whether a given move is legal (blocks self / descendant / no-op drops).
  canReparent?: (draggedId: number, targetId: number | null) => boolean;
}

// Shared drag state + handlers threaded down to every node.
interface DragCtx {
  draggedId: number | null;
  overId: number | 'root' | null;
  onDragStart: (id: number) => void;
  onDragEnd: () => void;
  setOver: (id: number | 'root' | null) => void;
  onReparent?: (draggedId: number, targetId: number | null) => void;
  canReparent?: (draggedId: number, targetId: number | null) => boolean;
}

export function IdeaTree({ nodes, selectedId, onSelect, onReparent, canReparent }: IdeaTreeProps) {
  const [draggedId, setDraggedId] = useState<number | null>(null);
  const [overId, setOverId] = useState<number | 'root' | null>(null);

  const ctx: DragCtx = {
    draggedId,
    overId,
    onDragStart: setDraggedId,
    onDragEnd: () => {
      setDraggedId(null);
      setOverId(null);
    },
    setOver: setOverId,
    onReparent,
    canReparent,
  };

  const rootActive =
    draggedId != null &&
    overId === 'root' &&
    (canReparent ? canReparent(draggedId, null) : true);

  // The root drop zone: a drop that isn't captured by a node lands here and
  // makes the idea top-level. Only accepts a drop while an item is dragging.
  const handleRootDragOver = (e: React.DragEvent) => {
    if (draggedId == null) return;
    e.preventDefault();
    setOverId('root');
  };
  const handleRootDrop = (e: React.DragEvent) => {
    if (draggedId == null) return;
    e.preventDefault();
    if (!canReparent || canReparent(draggedId, null)) {
      onReparent?.(draggedId, null);
    }
    setDraggedId(null);
    setOverId(null);
  };

  return (
    <ul
      className={`idea-tree list-unstyled mb-0 rounded${rootActive ? ' bg-info-subtle' : ''}`}
      style={{ minHeight: '100%' }}
      onDragOver={handleRootDragOver}
      onDrop={handleRootDrop}
    >
      {nodes.map((node) => (
        <IdeaTreeNode
          key={node.id}
          node={node}
          selectedId={selectedId}
          onSelect={onSelect}
          depth={0}
          ctx={ctx}
        />
      ))}
    </ul>
  );
}

interface NodeProps {
  node: IdeaNode;
  selectedId: number | null;
  onSelect: (id: number) => void;
  depth: number;
  ctx: DragCtx;
}

// Dot colour for a goal-graph node's status (Bootstrap text-* classes).
const STATUS_DOT: Record<string, string> = {
  draft: 'text-body-secondary',
  expanding: 'text-info',
  ready: 'text-primary',
  blocked: 'text-warning',
  running: 'text-info',
  done: 'text-success',
  failed: 'text-danger',
  abandoned: 'text-body-secondary',
};

function IdeaTreeNode({ node, selectedId, onSelect, depth, ctx }: NodeProps) {
  const [expanded, setExpanded] = useState(true);
  const isFolder = node.kind === 'folder';
  const hasChildren = node.children.length > 0;
  const isSelected = selectedId === node.id;
  const isGoalNode = node.node_type != null;

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    setExpanded((v) => !v);
  };

  const isDragging = ctx.draggedId === node.id;
  const dropAllowed =
    ctx.draggedId != null &&
    (ctx.canReparent ? ctx.canReparent(ctx.draggedId, node.id) : ctx.draggedId !== node.id);
  const isDropTarget = ctx.overId === node.id && dropAllowed;

  const handleDragStart = (e: React.DragEvent) => {
    e.stopPropagation();
    ctx.onDragStart(node.id);
    e.dataTransfer.effectAllowed = 'move';
    // A payload is required for the drag to start in some browsers.
    e.dataTransfer.setData('text/plain', String(node.id));
  };

  const handleDragOver = (e: React.DragEvent) => {
    if (ctx.draggedId == null) return;
    // Claim this drop so it reparents onto this node rather than the root.
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = dropAllowed ? 'move' : 'none';
    ctx.setOver(node.id);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.stopPropagation();
    if (ctx.overId === node.id) ctx.setOver(null);
  };

  const handleDrop = (e: React.DragEvent) => {
    if (ctx.draggedId == null) return;
    e.preventDefault();
    e.stopPropagation();
    if (dropAllowed) ctx.onReparent?.(ctx.draggedId, node.id);
    ctx.onDragEnd();
  };

  return (
    <li>
      <div
        draggable
        className={`d-flex align-items-center gap-1 py-1 px-1 rounded${
          isSelected ? ' bg-primary-subtle' : ''
        }${isDropTarget ? ' bg-info-subtle border border-info' : ''}`}
        style={{
          cursor: 'pointer',
          paddingLeft: depth * 16,
          opacity: isDragging ? 0.5 : 1,
        }}
        onClick={() => onSelect(node.id)}
        onDragStart={handleDragStart}
        onDragEnd={ctx.onDragEnd}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {hasChildren ? (
          <span onClick={toggle} className="d-inline-flex">
            <CIcon icon={expanded ? cilCaretBottom : cilCaretRight} size="sm" />
          </span>
        ) : (
          <span style={{ display: 'inline-block', width: 12 }} />
        )}
        <CIcon
          icon={isFolder ? (expanded && hasChildren ? cilFolderOpen : cilFolder) : cilLightbulb}
          className={
            isFolder
              ? 'text-warning'
              : isGoalNode
                ? (STATUS_DOT[node.node_status] ?? 'text-info')
                : 'text-info'
          }
        />
        <span className="flex-grow-1 text-truncate">
          {node.title}
          {isGoalNode && (
            <small className="text-body-secondary ms-1">· {node.node_type}</small>
          )}
        </span>
        {node.tasked && (
          <span title="Bound to a task" className="text-success">
            <CIcon icon={cilCheckAlt} size="sm" />
          </span>
        )}
      </div>
      {expanded && hasChildren && (
        <ul className="list-unstyled mb-0">
          {node.children.map((child) => (
            <IdeaTreeNode
              key={child.id}
              node={child}
              selectedId={selectedId}
              onSelect={onSelect}
              depth={depth + 1}
              ctx={ctx}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
