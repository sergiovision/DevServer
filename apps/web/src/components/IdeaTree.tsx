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
}

export function IdeaTree({ nodes, selectedId, onSelect }: IdeaTreeProps) {
  return (
    <ul className="idea-tree list-unstyled mb-0">
      {nodes.map((node) => (
        <IdeaTreeNode
          key={node.id}
          node={node}
          selectedId={selectedId}
          onSelect={onSelect}
          depth={0}
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
}

function IdeaTreeNode({ node, selectedId, onSelect, depth }: NodeProps) {
  const [expanded, setExpanded] = useState(true);
  const isFolder = node.kind === 'folder';
  const hasChildren = node.children.length > 0;
  const isSelected = selectedId === node.id;

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    setExpanded((v) => !v);
  };

  return (
    <li>
      <div
        className={`d-flex align-items-center gap-1 py-1 px-1 rounded${isSelected ? ' bg-primary-subtle' : ''}`}
        style={{ cursor: 'pointer', paddingLeft: depth * 16 }}
        onClick={() => onSelect(node.id)}
      >
        {isFolder && hasChildren ? (
          <span onClick={toggle} className="d-inline-flex">
            <CIcon icon={expanded ? cilCaretBottom : cilCaretRight} size="sm" />
          </span>
        ) : (
          <span style={{ display: 'inline-block', width: 12 }} />
        )}
        <CIcon
          icon={isFolder ? (expanded && hasChildren ? cilFolderOpen : cilFolder) : cilLightbulb}
          className={isFolder ? 'text-warning' : 'text-info'}
        />
        <span className="flex-grow-1 text-truncate">{node.title}</span>
        {node.tasked && (
          <span title="Converted to task" className="text-success">
            <CIcon icon={cilCheckAlt} size="sm" />
          </span>
        )}
      </div>
      {isFolder && expanded && hasChildren && (
        <ul className="list-unstyled mb-0">
          {node.children.map((child) => (
            <IdeaTreeNode
              key={child.id}
              node={child}
              selectedId={selectedId}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
