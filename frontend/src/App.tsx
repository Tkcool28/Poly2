import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Approvals from "./pages/Approvals";
import Health from "./pages/Health";
import Overview from "./pages/Overview";
import Portfolio from "./pages/Portfolio";
import Signals from "./pages/Signals";
import Wallets from "./pages/Wallets";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/wallets" element={<Wallets />} />
        <Route path="/signals" element={<Signals />} />
        <Route path="/portfolio" element={<Portfolio />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/health" element={<Health />} />
      </Route>
    </Routes>
  );
}
