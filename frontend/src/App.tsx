import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import EmptyState from "./pages/EmptyState";
import Login from "./pages/Login";
import Session from "./pages/Session";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Layout />}>
        <Route path="/" element={<EmptyState />} />
        <Route path="/session/:id" element={<Session />} />
      </Route>
    </Routes>
  );
}
