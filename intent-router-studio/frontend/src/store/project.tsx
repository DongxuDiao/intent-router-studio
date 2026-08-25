/** 当前项目上下文（localStorage 持久化）。 */
import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

interface ProjectCtx {
  projectId: string | null
  setProjectId: (id: string | null) => void
}

const Ctx = createContext<ProjectCtx>({ projectId: null, setProjectId: () => {} })

const KEY = 'irs.projectId'

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projectId, setProjectIdState] = useState<string | null>(() => localStorage.getItem(KEY))

  const setProjectId = (id: string | null) => {
    setProjectIdState(id)
    if (id) localStorage.setItem(KEY, id)
    else localStorage.removeItem(KEY)
  }

  return <Ctx.Provider value={{ projectId, setProjectId }}>{children}</Ctx.Provider>
}

export function useProject(): ProjectCtx {
  return useContext(Ctx)
}
