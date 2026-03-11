import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import RequireAuth from "./components/RequireAuth";
import EmptyState from "./pages/EmptyState";
import Landing from "./pages/Landing";
import Session from "./pages/Session";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/app" element={<EmptyState />} />
          <Route path="/app/session/:id" element={<Session />} />
        </Route>
      </Route>
    </Routes>
  );
}
