import React, { Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import RequireAuth from "./components/RequireAuth";
import Landing from "./pages/Landing";

/* Code-split authenticated routes so landing page visitors download only what they need. */
const EmptyState = React.lazy(() => import("./pages/EmptyState"));
const Session = React.lazy(() => import("./pages/Session"));
const Settings = React.lazy(() => import("./pages/Settings"));

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/app" element={<Suspense><EmptyState /></Suspense>} />
          <Route path="/app/session/:id" element={<Suspense><Session /></Suspense>} />
          <Route path="/app/settings" element={<Suspense><Settings /></Suspense>} />
        </Route>
      </Route>
    </Routes>
  );
}
