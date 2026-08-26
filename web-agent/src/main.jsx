import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Buyer from "./pages/Buyer.jsx";
import Audit from "./pages/Audit.jsx";
import "./index.css";

// Spec 2: the consumer app is buyer chat at / plus the audit view at /audit.
function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Buyer />} />
        <Route path="/run/:runId" element={<Buyer />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
