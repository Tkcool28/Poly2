import { NavLink, Outlet } from "react-router-dom";
import { api, useApi } from "../lib/api";
import SafetyBanner from "./SafetyBanner";

/**
 * Mobile-first shell: compact header up top, thumb-reach tab bar pinned
 * to the bottom (the primary way this app is used is an Android phone).
 */
const TABS = [
  {
    to: "/",
    label: "Home",
    icon: "M3 10.5 12 3l9 7.5M5 9.5V21h5v-6h4v6h5V9.5",
  },
  {
    to: "/signals",
    label: "Activity",
    icon: "M13 2 4.5 13.5H11L9.5 22 19 10h-6.5L13 2Z",
  },
  {
    to: "/portfolio",
    label: "Portfolio",
    icon: "M4 7h16v13H4zM8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
  },
  {
    to: "/wallets",
    label: "Wallets",
    icon: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1",
  },
  {
    to: "/approvals",
    label: "Review",
    icon: "M9 12l2 2 4-5M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18Z",
  },
];

export default function Layout() {
  const { data: queue } = useApi(api.approvalQueue, 30000);
  const pending = queue?.count ?? 0;

  return (
    <div className="flex min-h-screen flex-col">
      <SafetyBanner />
      <header className="border-b border-gray-800 px-4 py-3">
        <h1 className="text-lg font-bold">Polycopy</h1>
        <p className="text-xs text-gray-400">
          Copies smart Polymarket wallets — practice money only
        </p>
      </header>

      <main className="flex-1 px-4 pb-24 pt-4">
        <Outlet />
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-10 border-t border-gray-800 bg-gray-950/95 backdrop-blur">
        <div className="mx-auto flex max-w-lg">
          {TABS.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.to === "/"}
              className={({ isActive }) =>
                `relative flex min-h-[56px] flex-1 flex-col items-center justify-center gap-0.5 py-1 text-[11px] ${
                  isActive ? "text-white" : "text-gray-500"
                }`
              }
            >
              <svg
                viewBox="0 0 24 24"
                className="h-6 w-6"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.8}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d={tab.icon} />
              </svg>
              {tab.label}
              {tab.to === "/approvals" && pending > 0 && (
                <span className="absolute right-1/2 top-1 -mr-6 rounded-full bg-amber-500 px-1.5 text-[10px] font-bold text-black">
                  {pending}
                </span>
              )}
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  );
}
