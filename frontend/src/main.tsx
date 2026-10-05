import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import "./index.css";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import { SessionProvider, useSession } from "./session";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import NewInvestigation from "./pages/NewInvestigation";
import Investigations from "./pages/Investigations";
import InvestigationDetail from "./pages/InvestigationDetail";
import EvidenceExplorer from "./pages/EvidenceExplorer";
import Reviews from "./pages/Reviews";
import Catalog from "./pages/Catalog";
import Quality from "./pages/Quality";
import Admin from "./pages/Admin";

function Guard({ children }: { children: React.ReactNode }) {
  const { session, loading } = useSession();
  const loc = useLocation();
  if (loading) return <div className="p-8"><Spinner /></div>;
  if (!session) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return <>{children}</>;
}

function NotFound() {
  return <div className="text-sm text-muted">Page not found.</div>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SessionProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<Guard><Layout /></Guard>}>
            <Route index element={<Overview />} />
            <Route path="investigations" element={<Investigations />} />
            <Route path="investigations/new" element={<NewInvestigation />} />
            <Route path="investigations/:id" element={<InvestigationDetail />} />
            <Route path="evidence" element={<EvidenceExplorer />} />
            <Route path="reviews" element={<Reviews />} />
            <Route path="catalog" element={<Catalog />} />
            <Route path="quality" element={<Quality />} />
            <Route path="admin" element={<Admin />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </SessionProvider>
  </StrictMode>,
);
