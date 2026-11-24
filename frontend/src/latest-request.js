import {useCallback, useEffect, useRef} from 'react';

export function useLatestRequest() {
  const current = useRef(null);
  const abort = useCallback(() => current.current?.abort(), []);
  const begin = useCallback(() => {
    abort();
    current.current = new AbortController();
    return current.current.signal;
  }, [abort]);
  useEffect(() => abort, [abort]);
  return [begin, abort];
}
