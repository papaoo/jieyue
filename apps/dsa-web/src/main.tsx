import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './styles/workbench.css'
import App from './App.tsx'
import { ThemeProvider } from './components/theme/ThemeProvider'
import { MotionPreferenceProvider } from './contexts/MotionPreferenceContext'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <MotionPreferenceProvider>
        <App />
      </MotionPreferenceProvider>
    </ThemeProvider>
  </StrictMode>,
)
