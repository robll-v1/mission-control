import React from 'react'
import ReactDOM from 'react-dom/client'
import { App } from './App'
import { resolveUiMode } from './uiMode'
import { WorkbenchApp } from './workbench/WorkbenchApp'
import './styles.css'
import './workbench/workbench.css'

const uiMode = resolveUiMode()
document.documentElement.dataset.ui = uiMode

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {uiMode === 'legacy' ? <App /> : <WorkbenchApp />}
  </React.StrictMode>,
)
