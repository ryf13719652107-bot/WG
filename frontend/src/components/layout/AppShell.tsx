import { ReactNode } from 'react';
import Sidebar from './Sidebar';
import StatusBar from './StatusBar';

export default function AppShell({
  children,
  showLogout,
  onLogout,
}: {
  children: ReactNode;
  showLogout?: boolean;
  onLogout?: () => void;
}) {
  return (
    <div className="flex h-screen bg-gray-950 text-gray-100">
      <Sidebar showLogout={showLogout} onLogout={onLogout} />
      <div className="flex-1 flex flex-col overflow-hidden">
        <StatusBar />
        <main className="flex-1 overflow-auto p-4">{children}</main>
      </div>
    </div>
  );
}
