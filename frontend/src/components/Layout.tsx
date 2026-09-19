import { NavLink, Outlet } from "react-router-dom";
import SafetyBanner from "./SafetyBanner";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/wallets", label: "Wallets" },
  { to: "/signals", label: "Signals" },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/approvals", label: "Approvals" },
  { to: "/health", label: "System Health" },
];

export default function Layout() {
  return (
    <div className="min-h-screen">
      <SafetyBanner />
      <header className="border-b border-gray-800 px-6 py-4">
        <h1 className="text-xl font-bold">Polycopy</h1>
        <p className="text-sm text-gray-400">
          Smart-wallet copy-trading research platform
        </p>
      </header>
      <div className="flex">
        <nav className="w-48 shrink-0 border-r border-gray-800 p-4">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `block rounded px-3 py-2 text-sm ${
                  isActive
                    ? "bg-gray-800 font-semibold text-white"
                    : "text-gray-400 hover:text-white"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
