import type React from 'react';
import { useEffect, useState } from 'react';
import { ChevronLeft, Menu, PanelLeftOpen } from 'lucide-react';
import { Outlet, useLocation } from 'react-router-dom';
import { Drawer } from '../common/Drawer';
import { SidebarNav } from './SidebarNav';
import { DesktopUpdateIndicator } from './DesktopUpdateIndicator';
import { cn } from '../../utils/cn';
import { ThemeToggle } from '../theme/ThemeToggle';
import { UiLanguageToggle } from '../i18n/UiLanguageToggle';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { BRAND } from '../../config/brand';

type ShellProps = {
  children?: React.ReactNode;
};

export const Shell: React.FC<ShellProps> = ({ children }) => {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const { t, language } = useUiLanguage();
  const location = useLocation();

  useEffect(() => {
    if (!mobileOpen) {
      return undefined;
    }

    const handleResize = () => {
      if (window.innerWidth >= 1024) {
        setMobileOpen(false);
      }
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [mobileOpen]);

  return (
    <div className="workbench-shell min-h-screen bg-background text-foreground">
      <a className="workbench-skip-link" href="#workspace-content">{t('layout.skipToContent')}</a>
      <div className="pointer-events-none fixed inset-x-0 top-3 z-40 flex items-start justify-between px-3">
        <button
          type="button"
          onClick={() => setMobileOpen(true)}
          className="pointer-events-auto inline-flex h-10 w-10 items-center justify-center rounded-xl border border-border/70 bg-card/85 text-secondary-text shadow-soft-card backdrop-blur-md transition-colors hover:bg-hover hover:text-foreground lg:hidden"
          aria-label={t('layout.openNav')}
        >
          <Menu className="h-5 w-5" />
        </button>
        <div className="pointer-events-auto ml-auto flex items-center gap-2">
          <DesktopUpdateIndicator />
          <div className="flex items-center gap-2 lg:hidden">
            <UiLanguageToggle />
            <ThemeToggle />
          </div>
        </div>
      </div>

      <div className="workbench-frame mx-auto flex min-h-screen w-full max-w-[1800px] px-3 py-3 sm:px-4 sm:py-4 lg:px-5">
        <aside
          className={cn(
            'workbench-sidebar sticky top-3 z-40 hidden shrink-0 flex-col rounded-[1.25rem] border border-[var(--shell-sidebar-border)] bg-card/72 p-3 shadow-soft-card backdrop-blur-sm lg:flex',
            'h-[calc(100dvh-1.5rem)] self-start sm:top-4 sm:h-[calc(100dvh-2rem)]',
            collapsed ? 'w-[76px]' : 'w-[216px]'
          )}
          aria-label={t('layout.desktopSidebar')}
        >
          <SidebarNav collapsed={collapsed} onNavigate={() => setMobileOpen(false)} />
          <button type="button" className="workbench-collapse mt-3" onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? t('layout.expandSidebar') : t('layout.collapseSidebar')}>
            {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <><ChevronLeft className="h-4 w-4" /><span>{t('layout.collapseSidebar')}</span></>}
          </button>
        </aside>

        <main id="workspace-content" tabIndex={-1} className="workbench-main min-h-0 min-w-0 flex-1 pt-14 lg:pl-5 lg:pt-0 touch-pan-y">
          <header className="workbench-topbar">
            <div><span className="workbench-topbar-name">{BRAND.name}</span><span className="workbench-topbar-divider" aria-hidden="true">/</span><span>{language === 'zh' ? '研究工作台' : 'Research workspace'}</span></div>
            <span className="workbench-topbar-note">{language === 'zh' ? BRAND.tagline : 'Observe change. Test your thesis.'}</span>
          </header>
          <div key={location.pathname} className="workbench-route">{children ?? <Outlet />}</div>
        </main>
      </div>

      <Drawer
        isOpen={mobileOpen}
        onClose={() => setMobileOpen(false)}
        title={t('layout.navMenu')}
        width="max-w-xs"
        zIndex={90}
        side="left"
      >
        <SidebarNav onNavigate={() => setMobileOpen(false)} />
      </Drawer>
    </div>
  );
};
