import { useEffect, useState, useMemo } from 'react'
import * as Y from 'yjs'
import { WebsocketProvider } from 'y-websocket'

interface User {
  name: string
  color: string
}

interface CollaboratorInfo {
  clientId: number
  user: User
}

const colors = [
  '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4',
  '#FFEAA7', '#DDA0DD', '#98D8C8', '#F7DC6F',
  '#BB8FCE', '#85C1E9', '#F8B500', '#00CED1'
]

const getRandomColor = () => colors[Math.floor(Math.random() * colors.length)]

export function useCollaboration(
  documentId: string,
  userName: string = `User-${Math.floor(Math.random() * 1000)}`
) {
  const [isConnected, setIsConnected] = useState(false)
  const [collaborators, setCollaborators] = useState<CollaboratorInfo[]>([])
  const [isSynced, setIsSynced] = useState(false)
  const [provider, setProvider] = useState<WebsocketProvider | null>(null)

  const ydoc = useMemo(() => new Y.Doc(), [])

  useEffect(() => {
    const wsUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:4000/ws'

    const newProvider = new WebsocketProvider(wsUrl, documentId, ydoc, {
      connect: true,
    })

    setProvider(newProvider)

    // Set user info
    const userColor = getRandomColor()
    newProvider.awareness.setLocalStateField('user', {
      name: userName,
      color: userColor,
    })

    // Track connection status
    newProvider.on('status', ({ status }: { status: string }) => {
      setIsConnected(status === 'connected')
    })

    newProvider.on('sync', (synced: boolean) => {
      setIsSynced(synced)
    })

    // Track collaborators
    const updateCollaborators = () => {
      const states = newProvider.awareness.getStates()
      const collabs: CollaboratorInfo[] = []

      states.forEach((state, clientId) => {
        if (clientId !== ydoc.clientID && state.user) {
          collabs.push({
            clientId,
            user: state.user as User,
          })
        }
      })

      setCollaborators(collabs)
    }

    newProvider.awareness.on('change', updateCollaborators)
    updateCollaborators()

    return () => {
      newProvider.awareness.off('change', updateCollaborators)
      newProvider.destroy()
    }
  }, [documentId, userName, ydoc])

  return {
    ydoc,
    provider,
    isConnected,
    isSynced,
    collaborators,
  }
}
