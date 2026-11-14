import { useCallback, useEffect, useState } from "react";

export function usePath(): [string, (to: string) => void] {
  const [path, setPath] = useState(() => location.pathname);
  useEffect(() => {
    const onPop = () => setPath(location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = useCallback((to: string) => {
    history.pushState(null, "", to);
    setPath(to);
  }, []);
  return [path, go];
}
