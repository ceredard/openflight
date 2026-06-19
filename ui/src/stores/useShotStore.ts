import { create } from 'zustand';
import type { Shot } from '../types/shot';

/** Duration to keep isNewShot true — covers the longest animation (shot-glow: 2s) */
const NEW_SHOT_DURATION_MS = 2500;

interface ShotState {
  latestShot: Shot | null;
  shots: Shot[];
  isNewShot: boolean;
  shotVersion: number;
  addShot: (shot: Shot) => void;
  setShots: (shots: Shot[]) => void;
  clearShots: () => void;
  attachVideo: (shotNumber: number, videoPath: string, sessionId: string) => void;
}

export const useShotStore = create<ShotState>((set) => {
  let timerRef: ReturnType<typeof setTimeout> | null = null;

  return {
    latestShot: null,
    shots: [],
    isNewShot: false,
    shotVersion: 0,
    addShot: (shot) => {
      set((state) => {
        const updated = [...state.shots, shot];
        const newShots = updated.length > 200 ? updated.slice(-200) : updated;
        return {
          latestShot: shot,
          shots: newShots,
          isNewShot: true,
          shotVersion: state.shotVersion + 1,
        };
      });

      if (timerRef) clearTimeout(timerRef);
      timerRef = setTimeout(() => {
        set({ isNewShot: false });
      }, NEW_SHOT_DURATION_MS);
    },
    setShots: (newShots) => {
      set({
        shots: newShots,
        latestShot: newShots.length > 0 ? newShots[newShots.length - 1] : null,
      });
    },
    clearShots: () => {
      if (timerRef) clearTimeout(timerRef);
      set({
        latestShot: null,
        shots: [],
        isNewShot: false,
      });
    },
    attachVideo: (shotNumber, videoPath, sessionId) => {
      set((state) => {
        const shots = state.shots.map((shot) =>
          shot.shot_number === shotNumber
            ? { ...shot, video_path: videoPath, session_id: sessionId }
            : shot
        );
        const latestShot =
          state.latestShot?.shot_number === shotNumber
            ? { ...state.latestShot, video_path: videoPath, session_id: sessionId }
            : state.latestShot;
        return { shots, latestShot };
      });
    },
  };
});
