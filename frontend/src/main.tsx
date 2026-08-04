import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";
import "@/styles/sevps.css";
import "@/styles/ops-map.css";
import "@/styles/ops-map2.css";
import "@/styles/paramedic.css";
import "@/styles/driver.css";
import "@/styles/paramedic-portal.css";
import "@/styles/driver-portal.css";
import "@/styles/hospital-portal.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
