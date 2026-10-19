import { useEffect, useState } from 'react';
import { api, type ProjectDetail, type RaceView } from './api';
import { createReadResource } from './readResource';

const resource = createReadResource<RaceView>((key, signal) => api.race(JSON.parse(key)[0], signal));
export function raceRevision(detail: ProjectDetail) {
  return JSON.stringify([detail.project.config_version, detail.project.last_external_at,
    detail.jobs.map(j => [j.id, j.status]), detail.public_profile?.updated_at]);
}
export function useRace(projectId: string, revision: string) {
  const key = JSON.stringify([projectId, revision]);
  const [, render] = useState(0);
  useEffect(() => {
    const unsubscribe = resource.subscribe(key, () => render(n => n + 1));
    void resource.load(key);
    return unsubscribe;
  }, [key]);
  return { ...resource.snapshot(key), reload: () => resource.load(key, true) };
}
