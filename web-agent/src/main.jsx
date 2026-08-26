import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Buyer from "./pages/Buyer.jsx";
import "./index.css";

// Spec 2: the consumer app is buyer chat at / plus the audit view at /audit.
// /audit is Phase 8; until then it redirects rather than 404ing on a route the
// spec promises.
function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Buyer />} />
        <Route path="/run/:runId" element={<Buyer />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
