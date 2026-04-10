import { useEditor, EditorContent } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Link from '@tiptap/extension-link'
import Image from '@tiptap/extension-image'
import Underline from '@tiptap/extension-underline'
import TextAlign from '@tiptap/extension-text-align'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import Collaboration from '@tiptap/extension-collaboration'
import CollaborationCursor from '@tiptap/extension-collaboration-cursor'
import { useState, useMemo } from 'react'
import { EditorToolbar } from './EditorToolbar'
import { SlashCommand } from './extensions/SlashCommand'
import { WikiLink } from './extensions/WikiLink'
import { TableEmbedNode } from './extensions/TableEmbedNode'
import { TablePicker } from './TableEmbed'
import { CommentsPanel } from '../Panels/CommentsPanel'
import { TimelinePanel } from '../Panels/TimelinePanel'
import { useCollaboration } from '../../hooks/useCollaboration'

interface CollaborativeEditorProps {
  documentId: string
  userName?: string
  pageTitle?: string
  onNavigateToPage?: (pageName: string) => void
}

export function CollaborativeEditor({
  documentId,
  userName,
  pageTitle,
  onNavigateToPage,
}: CollaborativeEditorProps) {
  const [isCommentsOpen, setIsCommentsOpen] = useState(false)
  const [isTimelineOpen, setIsTimelineOpen] = useState(false)
  const [isTablePickerOpen, setIsTablePickerOpen] = useState(false)

  const displayName = userName || `User-${Math.floor(Math.random() * 1000)}`
  const userColor = useMemo(() => '#' + Math.floor(Math.random() * 16777215).toString(16).padStart(6, '0'), [])

  const { ydoc, provider, isConnected, collaborators } = useCollaboration(
    documentId,
    displayName
  )

  const extensions = useMemo(() => {
    const exts = [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        history: false,
      }),
      Underline,
      TextAlign.configure({
        types: ['heading', 'paragraph'],
      }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Placeholder.configure({
        placeholder: ({ node }) => {
          if (node.type.name === 'heading') return 'Заголовок'
          return 'Введите / для команд...'
        },
      }),
      Link.configure({
        openOnClick: false,
        HTMLAttributes: {
          class: 'text-primary underline cursor-pointer hover:text-primary-hover',
        },
      }),
      Image.configure({
        HTMLAttributes: { class: 'max-w-full rounded-lg' },
      }),
      SlashCommand.configure({
        onOpenTablePicker: () => setIsTablePickerOpen(true),
        onOpenAICommand: (command) => console.log('AI command:', command),
      }),
      TableEmbedNode,
      WikiLink.configure({
        onNavigate: onNavigateToPage,
      }),
      Collaboration.configure({
        document: ydoc,
      }),
    ]

    // Only add cursor extension when provider is ready
    if (provider) {
      exts.push(
        CollaborationCursor.configure({
          provider,
          user: {
            name: displayName,
            color: userColor,
          },
        })
      )
    }

    return exts
  }, [ydoc, provider, displayName, userColor, onNavigateToPage])

  const editor = useEditor({
    extensions,
    editorProps: {
      attributes: {
        class: 'prose prose-sm max-w-none focus:outline-none min-h-[400px]',
      },
    },
  }, [extensions])

  return (
    <div className="bg-white flex flex-col h-full relative">
      <EditorToolbar
        editor={editor}
        isCommentsOpen={isCommentsOpen}
        onToggleComments={() => setIsCommentsOpen(!isCommentsOpen)}
        isTimelineOpen={isTimelineOpen}
        onToggleTimeline={() => setIsTimelineOpen(!isTimelineOpen)}
        saveStatus="saved"
      />

      {/* Connection status bar */}
      <div className="flex items-center gap-2 px-4 py-1.5 border-b border-mws-gray-100 text-xs">
        <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
        <span className="text-mws-gray-500">
          {isConnected ? 'Подключено' : 'Отключено'}
        </span>
        {collaborators.length > 0 && (
          <>
            <span className="text-mws-gray-300">|</span>
            <div className="flex items-center gap-1">
              {collaborators.map((c) => (
                <div
                  key={c.clientId}
                  className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] text-white font-medium"
                  style={{ backgroundColor: c.user.color }}
                  title={c.user.name}
                >
                  {c.user.name.charAt(0).toUpperCase()}
                </div>
              ))}
              <span className="text-mws-gray-500 ml-1">
                +{collaborators.length} online
              </span>
            </div>
          </>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[700px] mx-auto py-12 px-6">
          {pageTitle && (
            <h1 className="text-3xl font-bold text-mws-gray-700 mb-6">
              {pageTitle}
            </h1>
          )}
          <EditorContent editor={editor} className="w-full" />
        </div>
      </div>

      <CommentsPanel
        isOpen={isCommentsOpen}
        onClose={() => setIsCommentsOpen(false)}
      />

      <TimelinePanel
        isOpen={isTimelineOpen}
        onClose={() => setIsTimelineOpen(false)}
      />

      <TablePicker
        isOpen={isTablePickerOpen}
        onClose={() => setIsTablePickerOpen(false)}
        onSelect={(datasheetId) => {
          if (editor) {
            editor.commands.insertTableEmbed(datasheetId)
          }
        }}
      />
    </div>
  )
}
