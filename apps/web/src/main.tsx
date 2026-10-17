import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route, Link } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Home, ProblemPage } from "./pages";
import { CodePage } from './CodePage';
import "./style.css";
const client = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <header>
          <Link className="brand" to="/">
            FocusMate <span>题目准备</span>
          </Link>
          <span className="local">本地工作区</span>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/problems/new" element={<ProblemPage />} />
            <Route path="/problems/:id/versions/:version/code" element={<CodePage />} />
            <Route
              path="/problems/:id/versions/:version/:step?"
              element={<ProblemPage />}
            />
            <Route
              path="*"
              element={
                <p>
                  没有找到这个页面。<Link to="/">返回首页</Link>
                </p>
              }
            />
          </Routes>
        </main>
        <footer>每弄懂一道题，都是一次进步。</footer>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
