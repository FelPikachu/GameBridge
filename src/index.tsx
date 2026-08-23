import { callable, definePlugin, openFilePicker, routerHook } from "@decky/api";
import {
  afterPatch, appDetailsClasses, appDetailsHeaderClasses,
  ButtonItem, createReactTreePatcher,
  ConfirmModal, DialogButton, fakeRenderComponent, findInReactTree, findInTree, findModuleByExport,
  Focusable, gamepadContextMenuClasses, MenuItem, Navigation, PanelSection, PanelSectionRow, ToggleField,
  replacePatch, showModal, Spinner, TextField, wrapReactType, staticClasses
} from "@decky/ui";
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { FaArrowUpRightFromSquare, FaBridge, FaFolderOpen, FaPlay } from "react-icons/fa6";
import { LuDownload, LuRefreshCw, LuUpload } from "react-icons/lu";
import { EPIC_GAMES_LOGO, MIHOYO_LAUNCHER_LOGO } from "./brand-assets";
import { localizeBackend, steamT, t } from "./i18n";

type ProviderSummary = {
  id: string;
  name: string;
  capabilities: Record<string, boolean>;
  status: { state: string; message?: string; version?: string; account?: string; action?: string; officialPage?: string; installer?: { sourceFilename?: string; sha256?: string } };
};

type Dashboard = {
  version: string;
  providerCount: number;
  gameCount: number;
  activeJobCount: number;
  providers: ProviderSummary[];
  runtime: { ready: boolean; umuInstalled: boolean; protonLayers: { name: string; path: string; recommended: boolean }[] };
  status: string;
};

type GameItem = {
  id: string;
  title: string;
  compatibility_status: string;
  provider_id: string;
  provider_name: string;
  external_game_id: string;
  region: string;
};

type GamePage = { items: GameItem[]; total: number; offset: number; limit: number };

type GameDetails = GameItem & {
  release_channel: string;
  description?: string;
  developer?: string;
  artwork_url?: string;
  hero_url?: string;
  header_url?: string;
  logo_url?: string;
  icon_url?: string;
  artwork_source?: "steam" | "epic" | "official" | "steamgriddb";
  artwork_language?: string;
  installed: boolean;
  launchable?: boolean;
  official_client_installed?: boolean;
  install_path?: string;
  installed_version?: string;
  latest_version?: string;
  update_available?: boolean;
  install_job?: InstallJob;
  executable?: string;
  steam_shortcut?: SteamShortcutProfile;
  channel_profile?: ChannelProfile;
  play_history?: { playtimeMinutes: number; lastPlayed: number };
};

type ChannelProfile = {
  current: "official" | "bilibili" | "unknown";
  official_ready: boolean;
  bilibili_ready: boolean;
  mode?: "sdk" | "qr";
};

type HoYoPlayChannelSelection = { current: "official" | "bilibili" | "global" };

type SteamShortcutProfile = {
  mode: "direct_executable" | "gamebridge_router";
  executable: string;
  start_directory: string;
  launch_options: string;
  compatibility_tool: string;
};

type SteamLibraryGame = GameItem & {
  artwork_url?: string;
  hero_url?: string;
  header_url?: string;
  logo_url?: string;
  icon_url?: string;
  artwork_source?: "steam" | "epic" | "official" | "steamgriddb";
  artwork_language?: string;
  developer?: string;
  installed: boolean;
  update_available?: boolean;
  steam_app_id?: number;
  native_steam_app_id?: number;
  executable?: string;
  launchable?: boolean;
  steam_shortcut?: SteamShortcutProfile;
};

type InstallJob = {
  id: string;
  state: string;
  progress: number;
  payload: { phase?: string; downloadedMiB?: number; speedMiBs?: number; eta?: string };
};

const getDashboard = callable<[], Dashboard>("get_dashboard");
const prepareCompatibility = callable<[], Dashboard["runtime"]>("prepare_compatibility");
const prepareHoYoPlayGameRuntime = callable<[gameId: string], { version: string; path: string }>("prepare_hoyoplay_game_runtime");
const installProviderTool = callable<[providerId: string], { version: string; path: string }>("install_provider_tool");
const automaticEpicLogin = callable<[], { state: string }>("automatic_epic_login");
const syncProviderLibrary = callable<[providerId: string], { count: number }>("sync_provider_library");
const refreshProviderStatus = callable<[providerId: string], { state: string }>("refresh_provider_status");
const logoutProvider = callable<[providerId: string], { state: string; browserSessionCleared?: boolean }>("logout_provider");
const runProviderInstaller = callable<[providerId: string], { state: string }>("run_provider_installer");
const downloadAndRunProviderInstaller = callable<[providerId: string], { state: string }>("download_and_run_provider_installer");
const downloadProviderInstaller = callable<[providerId: string], { sha256: string }>("download_provider_installer");
const listGames = callable<[query: string, offset: number, limit: number], GamePage>("list_games");
const getGameDetails = callable<[gameId: string], GameDetails>("game_details");
const switchHoYoPlayChannelProfile = callable<[gameId: string, channel: "official" | "bilibili"], ChannelProfile>("switch_hoyoplay_channel_profile");
const getHoYoPlayChannelSelection = callable<[], HoYoPlayChannelSelection>("hoyoplay_channel_selection");
const switchHoYoPlayChannelSelection = callable<[channel: "official" | "bilibili" | "global"], HoYoPlayChannelSelection>("switch_hoyoplay_channel_selection");
const startGameInstall = callable<[gameId: string, installPath?: string], { jobId: string }>("start_game_install");
const startGameUpdate = callable<[gameId: string], { jobId: string }>("start_game_update");
type StorageLocation = { id: string; name: string; path: string; free_bytes: number; enough_space: boolean; kind: "internal" | "drive" | "sd" };
const getStorageLocations = callable<[gameId: string], { required_bytes: number; download_bytes: number; locations: StorageLocation[]; recommended_id?: string }>("storage_locations");
const getInstallRequirements = callable<[gameId: string, path: string], { path: string; free_bytes: number; required_bytes: number; download_bytes: number; enough_space: boolean }>("install_requirements");
const getInstallJob = callable<[jobId: string], InstallJob>("get_install_job");
const pauseInstall = callable<[jobId: string], void>("pause_install");
const resumeInstall = callable<[jobId: string], void>("resume_install");
const uninstallGame = callable<[gameId: string], void>("uninstall_game");
type CleanupResult = { steamAppIds: number[]; removedGames: number; errors: string[] };
const cleanupBeforeUninstall = callable<[deleteGames: boolean], CleanupResult>("cleanup_before_uninstall");
type PlayHistoryExport = { path: string; count: number };
type PlayHistoryRecord = { steamAppId: number; playtimeMinutes: number; lastPlayed: number };
type PlayHistoryImport = { matched: number; updated: number; restartRequired: boolean; records: PlayHistoryRecord[]; nonEmpty: number };
type PlayHistoryBackup = { path: string; name: string; exportedAt: string; gameCount: number };
const playHistoryExports = callable<[], PlayHistoryBackup[]>("play_history_exports");
const exportPlayHistory = callable<[runtime: PlayHistoryRecord[]], PlayHistoryExport>("export_play_history");
const importPlayHistory = callable<[sourcePath: string, runtime: PlayHistoryRecord[]], PlayHistoryImport>("import_play_history");
const getSteamLibraryGames = callable<[], SteamLibraryGame[]>("steam_library_games");
const refreshSteamArtwork = callable<[gameId: string, language?: string], GameDetails>("refresh_steam_artwork");
type ArtworkSettings = { steamGridDbConfigured: boolean; steamGridDbLastValidationSucceeded: boolean };
const getArtworkSettings = callable<[], ArtworkSettings>("artwork_settings");
const saveSteamGridDbKey = callable<[key: string], ArtworkSettings>("save_steamgriddb_key");
const testSteamGridDbConnection = callable<[], { connected: boolean }>("test_steamgriddb_connection");
const downloadSteamGridDbArtwork = callable<[url: string], { base64: string; mimeType: string }>("download_steamgriddb_artwork");
const installSteamShortcutArtwork = callable<[providerId: string, externalGameId: string, steamAppId: number], { written: number; iconPath: string }>("install_steam_shortcut_artwork");
const registerSteamShortcut = callable<[providerId: string, externalGameId: string, steamAppId: number], void>("register_steam_shortcut");
const setRuntimeLanguage = callable<[providerId: string, externalGameId: string, language: string], void>("set_runtime_language");
type LaunchPreset = "default" | "lsfg" | "framegen" | "combined";
type ModifierAvailability = { lsfg: boolean; framegen: boolean };
const shortcutLaunchPreset = callable<[preset: LaunchPreset, providerId: string, externalGameId: string], string>("shortcut_launch_preset");
const getLaunchModifierAvailability = callable<[], ModifierAvailability>("launch_modifier_availability");
const GAMEBRIDGE_ENTRY = "/usr/bin/python3";
const getSteamGameDetails = callable<[steamAppId: number, title?: string], GameDetails | null>("steam_game_details");
let cachedDashboard: Dashboard | undefined;
let cachedSteamLibraryGames: SteamLibraryGame[] | undefined;
let cachedModifierAvailability: ModifierAvailability = { lsfg: false, framegen: false };
const EPIC_LOGIN_URL = "https://www.epicgames.com/id/login?redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi%2Fredirect%3FclientId%3D34a02cf8f4414e29b15921876da36f9a%26responseType%3Dcode";
const STEAMGRIDDB_API_URL = "https://www.steamgriddb.com/profile/preferences/api";
const STEAMGRIDDB_STEAM_LOGIN_URL = "https://steamcommunity.com/openid/login?openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0&openid.mode=checkid_setup&openid.realm=https%3A%2F%2Fwww.steamgriddb.com&openid.return_to=https%3A%2F%2Fwww.steamgriddb.com%2Flogin%2Fsteam";
const STEAM_ARTWORK = { Capsule: 0, Hero: 1, Logo: 2, Header: 3, Icon: 4 } as const;
const STEAMGRIDDB_IMAGE_HOSTS = new Set(["cdn.steamgriddb.com", "cdn2.steamgriddb.com", "s3.amazonaws.com"]);
const DASHBOARD_CARD_STYLE: CSSProperties = {
  width: "100%", padding: 12, borderRadius: 10, boxSizing: "border-box",
  background: "linear-gradient(135deg, rgba(255,255,255,.105), rgba(255,255,255,.045))",
  border: "1px solid rgba(255,255,255,.09)",
};
const DASHBOARD_STATUS_BADGE_STYLE: CSSProperties = {
  padding: "4px 8px",
  borderRadius: 7,
  fontSize: 11,
  fontWeight: 600,
};
const DASHBOARD_ICON_SIZE = 16;
const DASHBOARD_PRIMARY_BUTTON_STYLE: CSSProperties = {
  width: "100%", minWidth: 0, height: 44, margin: 0,
  color: "#fff", fontSize: 14, background: "#1a6dcc",
  border: "1px solid #3989e8", borderRadius: 8, whiteSpace: "nowrap",
};
const DASHBOARD_SECONDARY_BUTTON_STYLE: CSSProperties = {
  width: "100%", minWidth: 0, height: 44, color: "rgba(255,255,255,.86)", fontSize: 14,
  background: "rgba(255,255,255,.10)", border: "1px solid rgba(255,255,255,.08)", borderRadius: 8,
  whiteSpace: "nowrap",
};
const DASHBOARD_SEGMENTED_CONTAINER_STYLE: CSSProperties = {
  position: "relative",
  display: "grid",
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  padding: 2,
  overflow: "hidden",
  borderRadius: 7,
  background: "rgba(0,0,0,.32)",
  border: "1px solid rgba(255,255,255,.08)",
};
const DASHBOARD_SEGMENTED_SLIDER_STYLE: CSSProperties = {
  position: "absolute",
  top: 2,
  bottom: 2,
  width: "calc(50% - 2px)",
  borderRadius: 5,
  background: "#1a6dcc",
  boxShadow: "inset 0 0 0 1px rgba(255,255,255,.12)",
  transition: "transform 160ms ease",
};
const DASHBOARD_SEGMENTED_BUTTON_STYLE: CSSProperties = {
  zIndex: 1,
  width: "100%",
  minWidth: 0,
  height: 26,
  padding: 0,
  borderRadius: 5,
  fontSize: 11,
  border: 0,
  boxShadow: "none",
  background: "transparent",
  whiteSpace: "nowrap",
};
const DASHBOARD_ACTION_CLASS = "gamebridge-action";

function ProviderSteamLibraryPage({ providerIds }: { providerIds: string[] }) {
  const [games, setGames] = useState<SteamLibraryGame[] | undefined>(() => cachedSteamLibraryGames);
  const [error, setError] = useState<string>();
  const [openingGameId, setOpeningGameId] = useState<string>();
  const artworkAttempts = useRef(new Set<string>());
  const gameGrid = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    const reload = () => void withTimeout(getSteamLibraryGames(), 60000).then((value) => {
      cachedSteamLibraryGames = value;
      reconcileDirectShortcutTargets(value);
      if (active) setGames(value);
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : String(reason));
    });
    reload();
    window.addEventListener("gamebridge-artwork-updated", reload);
    return () => {
      active = false;
      window.removeEventListener("gamebridge-artwork-updated", reload);
    };
  }, []);

  useEffect(() => {
    const candidates = (games ?? []).filter((game) =>
      providerIds.includes(game.provider_id)
      && !game.artwork_url
      && !artworkAttempts.current.has(game.id));
    for (const game of candidates) {
      artworkAttempts.current.add(game.id);
      void withTimeout(refreshSteamArtwork(game.id), 20000).then((refreshed) => {
        setGames((current) => current?.map((item) => item.id === game.id ? { ...item, ...refreshed } : item));
        if (cachedSteamLibraryGames) {
          const cached = cachedSteamLibraryGames.find((item) => item.id === game.id);
          if (cached) Object.assign(cached, refreshed);
        }
      }).catch(() => undefined);
    }
  }, [games, providerIds]);

  const openNativeDetails = async (game: SteamLibraryGame) => {
    if (openingGameId) return;
    const existingAppId = game.steam_app_id;
    if (existingAppId && (window as any).appStore?.GetAppOverviewByAppID?.(existingAppId)) {
      // Opening an existing card is pure navigation. Revalidating metadata,
      // launch options or artwork here makes every visit look like a network
      // load and can invalidate Steam's native library cache.
      Navigation.Navigate(`/library/app/${existingAppId}`);
      return;
    }
    try {
      setOpeningGameId(game.id);
      // Only unresolved cards need fresh state before their first shortcut is
      // created. Existing shortcuts take the immediate path above.
      const fresh = await getGameDetails(game.id);
      Object.assign(game, fresh);
      let appId = game.steam_app_id;
      let createdShortcut = false;
      if (appId && (window as any).appStore?.GetAppOverviewByAppID?.(appId)) {
        Navigation.Navigate(`/library/app/${appId}`);
        return;
      }
      if (appId && !(window as any).appStore?.GetAppOverviewByAppID?.(appId)) {
        // Steam can delete a non-Steam shortcut while GameBridge still holds
        // its old AppID. Recreate it instead of navigating to a missing route.
        appId = undefined;
        game.steam_app_id = undefined;
      }
      if (!appId) {
        const nativeAppId = findNativeSteamApp(game);
        if (nativeAppId) {
          Navigation.Navigate(`/library/app/${nativeAppId}`);
          return;
        }
        if (game.provider_id === "epic") {
          const gameLanguage = steamGameLanguage();
          await setRuntimeLanguage(game.provider_id, game.external_game_id, gameLanguage);
        }
        appId = await SteamClient.Apps.AddShortcut(
          game.title,
          GAMEBRIDGE_ENTRY,
          "/home/deck/homebrew/plugins/GameBridge",
          `"gamebridge/launcher.py" --provider ${game.provider_id} --game-id "${game.external_game_id}"`,
        );
        if (!appId) throw new Error(t("shortcutFailed"));
        SteamClient.Apps.SetShortcutName(appId, game.title);
        applyManagedShortcutTarget(appId, game);
        await registerSteamShortcut(game.provider_id, game.external_game_id, appId);
        game.steam_app_id = appId;
        await waitForSteamShortcut(appId);
        createdShortcut = true;
      }
      // Re-assert the title for shortcuts created by earlier GameBridge builds.
      SteamClient.Apps.SetShortcutName(appId, game.title);
      applyManagedShortcutTarget(appId, game);
      await disableSteamOverlayForShortcut(appId);
      if (createdShortcut) {
        if (!game.artwork_url) {
          try {
            const refreshed = await withTimeout(
              refreshSteamArtwork(game.id), 20000,
            );
            Object.assign(game, refreshed);
            if (cachedSteamLibraryGames) {
              const cached = cachedSteamLibraryGames.find((item) => item.id === game.id);
              if (cached) Object.assign(cached, refreshed);
            }
          } catch (reason) {
            console.warn("[GameBridge] Steam artwork lookup failed; using provider artwork", reason);
          }
        }
        const artworkFingerprint = JSON.stringify([
          "full-artwork-v7-stable", game.artwork_url ?? "", game.hero_url ?? "", game.header_url ?? "", game.logo_url ?? "", game.icon_url ?? "", game.title,
        ]);
        const artworkKey = `gamebridge.artwork.${appId}`;
        try {
          let nativeFilesInstalled = true;
          if (usesSteamGridDbBackend(game)) {
            const installed = await withTimeout(
              installSteamShortcutArtwork(game.provider_id, game.external_game_id, appId),
              120000,
            );
            nativeFilesInstalled = installed.written >= 5;
            // Steam's shortcut API documents PNG/TGA paths. ICO files are instead
            // decoded below and installed through the custom Icon asset type.
            if (installed.iconPath.endsWith(".png")) {
              SteamClient.Apps.SetShortcutIcon(appId, installed.iconPath);
            }
            SteamClient.Apps.SetShortcutName(appId, game.title);
          }
          if (nativeFilesInstalled && await applySteamArtwork(appId, game)) {
            localStorage.setItem(artworkKey, artworkFingerprint);
          } else {
            localStorage.removeItem(artworkKey);
          }
        } catch (reason) {
          localStorage.removeItem(artworkKey);
          console.warn("[GameBridge] Steam artwork update failed", reason);
        }
      }
      Navigation.Navigate(`/library/app/${appId}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setOpeningGameId(undefined);
    }
  };

  const moveGameGridFocus = (direction: "up" | "down" | "left" | "right") => {
    const grid = gameGrid.current;
    if (!grid) return false;
    const cards = Array.from(grid.querySelectorAll<HTMLElement>('[data-gamebridge-library-card="true"]'));
    const activeElement = grid.ownerDocument.activeElement;
    const current = cards.find((card) =>
      card === activeElement || card.contains(activeElement) || card.classList.contains("gpfocus"),
    );
    if (!current) return false;
    const origin = current.getBoundingClientRect();
    const originX = origin.left + origin.width / 2;
    const originY = origin.top + origin.height / 2;
    const horizontal = direction === "left" || direction === "right";
    const candidates = cards.filter((card) => {
      if (card === current) return false;
      const rect = card.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      if (direction === "left") return x < originX && Math.abs(y - originY) < origin.height / 2;
      if (direction === "right") return x > originX && Math.abs(y - originY) < origin.height / 2;
      if (direction === "up") return y < originY;
      return y > originY;
    });
    const next = candidates.sort((left, right) => {
      const score = (card: HTMLElement) => {
        const rect = card.getBoundingClientRect();
        const deltaX = Math.abs(rect.left + rect.width / 2 - originX);
        const deltaY = Math.abs(rect.top + rect.height / 2 - originY);
        return horizontal ? deltaX + deltaY * 100 : deltaY + deltaX * 0.35;
      };
      return score(left) - score(right);
    })[0];
    if (!next && direction === "up") {
      const selectedTab = Array.from(grid.ownerDocument.querySelectorAll<HTMLElement>('[role="tab"][aria-selected="true"]'))
        .find((candidate) => candidate.getClientRects().length > 0);
      if (!selectedTab) return false;
      selectedTab.focus();
      return true;
    }
    if (!next) return false;
    next.focus();
    return true;
  };

  const handleGridKeyDown = (event: any) => {
    const direction = ({ ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" } as const)[event.key as "ArrowUp"];
    if (!direction || !moveGameGridFocus(direction)) return;
    event.preventDefault();
    event.stopPropagation();
  };

  const handleGridGamepadDirection = (event: any) => {
    const direction = ({ 9: "up", 10: "down", 11: "left", 12: "right" } as const)[event.detail?.button as 9];
    if (!direction || !moveGameGridFocus(direction)) return;
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: "12px 8px 90px", boxSizing: "border-box" }}>
      {!games && !error && <div style={{ display: "flex", justifyContent: "center", paddingTop: 80 }}><Spinner /></div>}
      {error && <div style={{ color: "#ff8080", fontSize: 18 }}>{t("libraryReadFailed", { error: localizeBackend(error) ?? error })}</div>}
      {games && (
        <Focusable ref={gameGrid} flow-children="grid" onKeyDown={handleGridKeyDown} onGamepadDirection={handleGridGamepadDirection} style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, 172px)",
          justifyContent: "space-between",
          columnGap: 24,
          rowGap: 43,
        }}>
          {games.filter((game) => providerIds.includes(game.provider_id)).map((game) => (
            <Focusable
              key={game.id}
              data-gamebridge-library-card="true"
              onActivate={() => void openNativeDetails(game)}
              onClick={() => void openNativeDetails(game)}
              style={{ width: 172, height: 258, overflow: "hidden", background: "#20242b", position: "relative" }}
            >
              {game.artwork_url
                ? <OfficialGameCover game={game} />
                : <GeneratedGameCover game={game} />}
              {openingGameId === game.id && <div style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", background: "rgba(0,0,0,.72)" }}><Spinner /></div>}
            </Focusable>
          ))}
        </Focusable>
      )}
    </div>
  );
}

function providerCoverColors(game: SteamLibraryGame): [string, string] {
  const palettes: Record<string, [string, string]> = {
    hk4e_cn: ["#2476b8", "#73c6d9"], hkrpg_cn: ["#28235f", "#7c63c8"],
    nap_cn: ["#242729", "#c9d52a"], bh3_cn: ["#3150a8", "#ca75b9"],
    hk4e_global: ["#2476b8", "#73c6d9"], hkrpg_global: ["#28235f", "#7c63c8"],
    nap_global: ["#242729", "#c9d52a"], bh3_global: ["#3150a8", "#ca75b9"],
  };
  return palettes[game.external_game_id] ?? ["#26364a", "#58799c"];
}

function GeneratedGameCover({ game }: { game: SteamLibraryGame }) {
  const [start, end] = providerCoverColors(game);
  const provider = localizeBackend(game.provider_name) ?? game.provider_name;
  return <div style={{
    width: "100%", height: "100%", boxSizing: "border-box", padding: "22px 16px",
    display: "flex", flexDirection: "column", justifyContent: "flex-end", overflow: "hidden",
    background: `radial-gradient(circle at 78% 18%, rgba(255,255,255,.3), transparent 24%), linear-gradient(145deg, ${start}, ${end})`,
    color: "white", textShadow: "0 2px 8px rgba(0,0,0,.6)",
  }}>
    <div style={{ fontSize: 12, opacity: .78, marginBottom: 8 }}>{provider}</div>
    <div style={{ fontSize: 22, lineHeight: 1.12, fontWeight: 800 }}>{game.title}</div>
  </div>;
}

function OfficialGameCover({ game }: { game: SteamLibraryGame }) {
  const focalPoint = officialCoverFocalPoint(game);
  return <div style={{ width: "100%", height: "100%", position: "relative", overflow: "hidden", background: "#15191f" }}>
    <img src={game.artwork_url} alt={game.title} style={{
      position: "absolute", inset: 0, width: "100%", height: "100%",
      display: "block", objectFit: "cover", objectPosition: `${focalPoint}% center`,
    }} />
    <div style={{
      position: "absolute", inset: 0,
      background: "linear-gradient(to bottom, rgba(0,0,0,0) 52%, rgba(8,11,16,.28) 69%, rgba(8,11,16,.94) 100%)",
    }} />
    <div style={{
      position: "absolute", left: 14, right: 12, bottom: 15, color: "white",
      fontSize: 21, lineHeight: 1.12, fontWeight: 800, textShadow: "0 2px 8px rgba(0,0,0,.9)",
    }}>{game.title}</div>
  </div>;
}

function officialCoverFocalPoint(game: SteamLibraryGame): number {
  const points: Record<string, number> = {
    hk4e_cn: 68, nap_cn: 60, hkrpg_cn: 55, bh3_cn: 58,
    hk4e_global: 68, nap_global: 60, hkrpg_global: 55, bh3_global: 58,
  };
  return points[game.external_game_id] ?? 50;
}

function findNativeSteamApp(game: SteamLibraryGame): number | undefined {
  const apps = (window as any).appStore?.m_mapApps;
  if (!apps?.values) return undefined;
  if (game.native_steam_app_id) {
    for (const app of apps.values()) {
      const appId = Number(app?.appid ?? app?.appID ?? 0);
      if (appId === game.native_steam_app_id) return appId;
    }
  }
  const wanted = game.title.normalize("NFKC").trim().toLocaleLowerCase();
  for (const app of apps.values()) {
    const appId = Number(app?.appid ?? app?.appID ?? 0);
    const name = String(app?.display_name ?? app?.strDisplayName ?? app?.name ?? "");
    if (appId > 0 && appId < 2_000_000_000
      && name.normalize("NFKC").trim().toLocaleLowerCase() === wanted) return appId;
  }
  return undefined;
}

async function waitForSteamShortcut(appId: number) {
  // AddShortcut triggers a native library rebuild. Navigating during that rebuild
  // briefly opens the detail page and then Steam replaces it with the home route.
  const started = Date.now();
  while (Date.now() - started < 5000) {
    const overview = (window as any).appStore?.GetAppOverviewByAppID?.(appId);
    if (overview && Date.now() - started >= 1400) return;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
}

async function waitForShortcutGameId(appId: number): Promise<string | undefined> {
  const started = Date.now();
  while (Date.now() - started < 5000) {
    const gameId = (window as any).appStore?.m_mapApps?.get?.(appId)?.gameid;
    if (typeof gameId === "string" && gameId.length > 0) return gameId;
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  return undefined;
}

function shortcutRunGameId(appId: number): string {
  const registered = (window as any).appStore?.m_mapApps?.get?.(appId)?.gameid;
  if (typeof registered === "string" && registered.length > 0) return registered;
  return ((BigInt(appId) << 32n) | 0x02000000n).toString();
}

function scheduleTemporaryShortcutCleanup(appId: number): () => void {
  const apps = SteamClient.Apps as any;
  let sawRunning = false;
  let removed = false;
  const cleanup: Array<() => void> = [];
  const remove = () => {
    if (removed) return;
    removed = true;
    cleanup.forEach((dispose) => dispose());
    try { apps.RemoveShortcut?.(appId); } catch (_) { /* already removed */ }
  };
  const subscription = (SteamClient as any).GameSessions?.RegisterForAppLifetimeNotifications?.((event: any) => {
    if (event.unAppID !== appId) return;
    if (event.bRunning) sawRunning = true;
    else if (sawRunning) remove();
  });
  if (subscription) cleanup.push(() => subscription.unregister());
  const timer = window.setTimeout(remove, 10 * 60 * 1000);
  cleanup.push(() => window.clearTimeout(timer));
  return remove;
}

async function launchProviderThroughSteam(provider: ProviderSummary, mode: "installer" | "launcher") {
  const storageKey = `gamebridge.providerShortcut.${provider.id}`;
  const apps = SteamClient.Apps as any;
  const legacyAppId = Number(localStorage.getItem(storageKey) ?? 0) || undefined;
  localStorage.removeItem(storageKey);
  if (legacyAppId) {
    try { await apps.RemoveShortcut?.(legacyAppId); } catch (_) { /* already removed */ }
  }
  const title = `${localizeBackend(provider.name) ?? provider.name} · GameBridge`;
  const launchOptions = `"gamebridge/launcher.py" --provider ${provider.id} --game-id ${mode}`;
  const appId = await SteamClient.Apps.AddShortcut(
    title,
    GAMEBRIDGE_ENTRY,
    "/home/deck/homebrew/plugins/GameBridge",
    launchOptions,
  );
  if (!appId) throw new Error(t("shortcutFailed"));
  await waitForSteamShortcut(appId);
  SteamClient.Apps.SetShortcutName(appId, title);
  SteamClient.Apps.SetShortcutExe(appId, GAMEBRIDGE_ENTRY);
  SteamClient.Apps.SetShortcutStartDir(appId, "/home/deck/homebrew/plugins/GameBridge");
  setShortcutLaunchOptions(appId, launchOptions);
  if (typeof apps.RunGame !== "function") throw new Error("Steam RunGame API unavailable");
  const gameId = await waitForShortcutGameId(appId) ?? shortcutRunGameId(appId);
  const removeTemporaryShortcut = scheduleTemporaryShortcutCleanup(appId);
  try {
    await apps.RunGame(gameId, "", -1, 100);
  } catch (reason) {
    removeTemporaryShortcut();
    throw reason;
  }
}

async function disableSteamOverlayForShortcut(appId: number) {
  const apps = SteamClient.Apps as any;
  const detailsStore = (window as any).appDetailsStore;
  if (typeof apps.ToggleEnableSteamOverlayForApp !== "function" || !detailsStore?.GetAppDetails) return;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    const enabled = detailsStore.GetAppDetails(appId)?.bOverlayEnabled;
    if (enabled === false) return;
    if (enabled === true) {
      await apps.ToggleEnableSteamOverlayForApp(appId);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
}

function setShortcutLaunchOptions(appId: number, value: string): void {
  const apps = SteamClient.Apps as any;
  if (typeof apps.SetShortcutLaunchOptions === "function") {
    try {
      apps.SetShortcutLaunchOptions(appId, value);
      return;
    } catch (_) {
      // Fall through for Steam builds which expose the shortcut method but
      // reject it at runtime.
    }
  }
  if (typeof apps.SetAppLaunchOptions === "function") {
    apps.SetAppLaunchOptions(appId, value);
    return;
  }
  throw new Error("Steam launch-options API unavailable");
}

function managedShortcutForApp(appId: number): SteamLibraryGame | undefined {
  return cachedSteamLibraryGames?.find((game) => game.steam_app_id === appId);
}

async function applyIntegratedLaunchPreset(
  appId: number,
  game: SteamLibraryGame,
  preset: LaunchPreset,
): Promise<void> {
  const isHoYoPlayGame = game.provider_id === "mihoyo_cn"
    || game.provider_id === "hoyoplay_global";
  if (isHoYoPlayGame && game.steam_shortcut) {
    // Regional HoYoPlay cards use a stable unified route (for example,
    // ``mihoyo + genshin``). Never reconstruct that route from the current
    // provider/game IDs: doing so changes its Prefix and can make it unlaunchable.
    applyManagedShortcutTarget(appId, game);
    if (preset !== "default") {
      const prefixes: Record<Exclude<LaunchPreset, "default">, string> = {
        lsfg: "~/lsfg %command%",
        framegen: "WINEDLLOVERRIDES=dxgi=n,b SteamDeck=0 %command%",
        combined: "~/fgmod/fgmod ~/lsfg %command%",
      };
      setShortcutLaunchOptions(
        appId,
        `${prefixes[preset]} ${game.steam_shortcut.launch_options}`,
      );
    }
  } else {
    const launchOptions = await shortcutLaunchPreset(
      preset, game.provider_id, game.external_game_id,
    );
    SteamClient.Apps.SetShortcutExe(appId, GAMEBRIDGE_ENTRY);
    SteamClient.Apps.SetShortcutStartDir(appId, "/home/deck/homebrew/plugins/GameBridge");
    setShortcutLaunchOptions(appId, launchOptions);
  }
  void setRuntimeLanguage(
    game.provider_id, game.external_game_id, steamGameLanguage(),
  ).catch((reason) => console.warn("GameBridge language sync failed", reason));
  await disableSteamOverlayForShortcut(appId);
  showModal(
    <ConfirmModal
      strTitle={preset === "default" ? t("repairShortcutTitle") : t("launchPresetTitle")}
      strDescription={preset === "default"
        ? t("repairShortcutDescription", { title: game.title })
        : t("launchPresetDescription", { title: game.title })}
      strOKButtonText={steamT("#Button_OK", "continue")}
    />,
  );
}

function scheduleIntegratedLaunchPreset(
  appId: number,
  game: SteamLibraryGame,
  preset: LaunchPreset,
): void {
  window.setTimeout(() => {
    void applyIntegratedLaunchPreset(appId, game, preset).catch((reason) => {
      showModal(
        <ConfirmModal
          strTitle={preset === "default" ? t("repairShortcutTitle") : t("launchPresetTitle")}
          strDescription={reason instanceof Error ? reason.message : String(reason)}
          strOKButtonText={steamT("#Button_OK", "continue")}
        />,
      );
    });
  }, 100);
}

function confirmIntegratedUninstall(game: SteamLibraryGame): void {
  showModal(
    <ConfirmModal
      strTitle={t("uninstallTitle", { title: game.title })}
      strDescription={t("uninstallDescription")}
      strOKButtonText={steamT("#GameAction_Uninstall", "uninstall")}
      strCancelButtonText={steamT("#Button_Cancel", "cancel")}
      onOK={() => void uninstallGame(game.id)}
    />,
  );
}

async function openOfficialLauncherToUninstall(game: SteamLibraryGame): Promise<void> {
  let providerId = game.provider_id;
  if (providerId === "mihoyo_cn") {
    const selection = await getHoYoPlayChannelSelection();
    if (selection.current === "global") providerId = "hoyoplay_global";
  }
  const dashboard = cachedDashboard ?? await getDashboard();
  cachedDashboard = dashboard;
  const provider = dashboard.providers.find((candidate) => candidate.id === providerId);
  if (!provider) throw new Error(`Provider unavailable: ${providerId}`);
  await launchProviderThroughSteam(provider, "launcher");
}

function scheduleOfficialLauncherToUninstall(game: SteamLibraryGame): void {
  window.setTimeout(() => {
    void openOfficialLauncherToUninstall(game).catch((reason) => {
      showModal(
        <ConfirmModal
          strTitle={t("uninstallInOfficialClient")}
          strDescription={reason instanceof Error ? reason.message : String(reason)}
          strOKButtonText={steamT("#Button_OK", "continue")}
        />,
      );
    });
  }, 100);
}

function applyIntegratedManagementItems(menuItems: any[], appId: number): void {
  if (!Array.isArray(menuItems)) return;
  for (let index = menuItems.length - 1; index >= 0; index -= 1) {
    if (String(menuItems[index]?.key ?? "").startsWith("gamebridge-management-")) {
      menuItems.splice(index, 1);
    }
  }
  const removeIndex = menuItems.findIndex((item) => item?.key === "RemoveShortcut");
  if (removeIndex < 0) return;
  const game = managedShortcutForApp(appId);
  if (!game) return;
  const isOfficialLauncherGame = game.provider_id === "mihoyo_cn"
    || game.provider_id === "hoyoplay_global";
  // HoYoPlay owns its game files. Hide Steam's generic remove-shortcut action
  // so it cannot be mistaken for the official launcher's uninstall workflow.
  let insertionIndex = removeIndex;
  if (isOfficialLauncherGame) {
    menuItems.splice(removeIndex, 1);
  } else if (game.installed) {
    const nativeRemove = menuItems[removeIndex];
    menuItems[removeIndex] = window.SP_REACT.cloneElement(
      nativeRemove,
      { onSelected: () => confirmIntegratedUninstall(game) },
      steamT("#GameAction_Uninstall", "uninstall"),
    );
    insertionIndex += 1;
  } else {
    menuItems.splice(removeIndex, 1);
  }
  const entries = [];
  if (isOfficialLauncherGame && game.installed) entries.push(
    <MenuItem
      key="gamebridge-management-official-uninstall"
      onSelected={() => scheduleOfficialLauncherToUninstall(game)}
    >
      {t("uninstallInOfficialClient")}
    </MenuItem>,
  );
  entries.push(
    <MenuItem
      key="gamebridge-management-default"
      onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "default")}
    >
      {t("repairShortcut")}
    </MenuItem>,
  );
  if (game.installed && cachedModifierAvailability.lsfg) entries.push(
    <MenuItem
      key="gamebridge-management-lsfg"
      onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "lsfg")}
    >
      {t("enableLsfg")}
    </MenuItem>,
  );
  if (game.installed && cachedModifierAvailability.framegen) entries.push(
    <MenuItem
      key="gamebridge-management-framegen"
      onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "framegen")}
    >
      {t("enableDeckyFramegen")}
    </MenuItem>,
  );
  if (game.installed && cachedModifierAvailability.lsfg && cachedModifierAvailability.framegen) entries.push(
    <MenuItem
      key="gamebridge-management-combined"
      onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "combined")}
    >
      {t("enableLsfgAndFramegen")}
    </MenuItem>,
  );
  menuItems.splice(insertionIndex, 0, ...entries);
}

function applySteamManagementMenuPatch(): () => void {
  const module = findModuleByExport((candidate: any) =>
    candidate?.toString?.().includes("().LibraryContextMenu"));
  const renderComponent = Object.values(module ?? {}).find((candidate: any) =>
    candidate?.toString?.().includes("navigator:"));
  if (typeof renderComponent !== "function") return () => undefined;
  const LibraryContextMenu = fakeRenderComponent(renderComponent).type;
  if (!LibraryContextMenu?.prototype?.render) return () => undefined;
  let activeAppId = 0;
  let menuPanelPatched = false;
  const patches: { unpatch(): void }[] = [];
  const patchedMenuTypes = new WeakSet<object>();
  const patchMenuType = (menuType: any) => {
    if (!menuType?.prototype?.render || patchedMenuTypes.has(menuType)) return;
    patchedMenuTypes.add(menuType);
    menuPanelPatched = true;
    patches.push(afterPatch(menuType.prototype, "render", (_args, rendered: any) => {
      const items = rendered?.props?.children?.[0];
      applyIntegratedManagementItems(items, activeAppId);
      return rendered;
    }));
    if (typeof menuType.prototype.shouldComponentUpdate === "function") {
      patches.push(afterPatch(
        menuType.prototype,
        "shouldComponentUpdate",
        ([nextProps], shouldUpdate: boolean) => {
          if (shouldUpdate === true) applyIntegratedManagementItems(nextProps?.children, activeAppId);
          return shouldUpdate;
        },
      ));
    }
  };
  patches.push(afterPatch(LibraryContextMenu.prototype, "render", (_args, component: any) => {
    const overview = component?._owner?.pendingProps?.overview
      ?? findInTree(component?.props?.children, (node: any) => node?.app?.appid, {
        walkable: ["props", "children"],
      })?.app;
    const appId = Number(overview?.appid);
    if (Number.isFinite(appId)) activeAppId = appId;
    const componentType = component?.type;
    if (!menuPanelPatched && typeof componentType === "function") {
      patches.push(afterPatch(component, "type", (_innerArgs, rendered: any) => {
        patchMenuType(rendered?.type);
        applyIntegratedManagementItems(component?.props?.children, activeAppId);
        return rendered;
      }));
    } else {
      applyIntegratedManagementItems(component?.props?.children, activeAppId);
    }
    return component;
  }));
  return () => patches.splice(0).reverse().forEach((patch) => patch.unpatch());
}

function applyManagedShortcutTarget(appId: number, game: SteamLibraryGame | GameDetails): void {
  const profile = game.steam_shortcut;
  if (profile?.mode === "direct_executable") {
    SteamClient.Apps.SetShortcutExe(appId, `"${profile.executable}"`);
    SteamClient.Apps.SetShortcutStartDir(appId, profile.start_directory);
    setShortcutLaunchOptions(appId, profile.launch_options);
    const apps = SteamClient.Apps as any;
    if (typeof apps.SpecifyCompatTool === "function") {
      apps.SpecifyCompatTool(appId, profile.compatibility_tool);
    }
    return;
  }
  if (profile?.mode === "gamebridge_router") {
    SteamClient.Apps.SetShortcutExe(appId, profile.executable);
    SteamClient.Apps.SetShortcutStartDir(appId, profile.start_directory);
    setShortcutLaunchOptions(appId, profile.launch_options);
    const apps = SteamClient.Apps as any;
    if (typeof apps.SpecifyCompatTool === "function") apps.SpecifyCompatTool(appId, "");
    return;
  }
  SteamClient.Apps.SetShortcutExe(appId, GAMEBRIDGE_ENTRY);
  SteamClient.Apps.SetShortcutStartDir(appId, "/home/deck/homebrew/plugins/GameBridge");
  setShortcutLaunchOptions(
    appId,
    `"gamebridge/launcher.py" --provider ${game.provider_id} --game-id "${game.external_game_id}"`,
  );
}

function reconcileDirectShortcutTargets(games: SteamLibraryGame[]): void {
  for (const game of games) {
    const appId = game.steam_app_id;
    const profile = game.steam_shortcut;
    if (!appId || !profile) continue;
    if (!(window as any).appStore?.GetAppOverviewByAppID?.(appId)) continue;
    const fingerprint = JSON.stringify([
      profile.mode,
      profile.executable,
      profile.start_directory,
      profile.launch_options,
      profile.compatibility_tool,
    ]);
    const storageKey = `gamebridge.shortcutTarget.${appId}`;
    if (localStorage.getItem(storageKey) === fingerprint) continue;
    try {
      applyManagedShortcutTarget(appId, game);
      localStorage.setItem(storageKey, fingerprint);
    } catch (reason) {
      console.warn("[GameBridge] Managed shortcut target update failed", reason);
    }
  }
}

function steamGameLanguage(): string {
  const candidates = [document.documentElement.lang, ...navigator.languages]
    .filter(Boolean)
    .map((value) => value.toLowerCase());
  const locale = candidates[0] ?? "en";
  if (locale.includes("tchinese") || locale.startsWith("zh-tw") || locale.startsWith("zh-hk")) return "zh-TW";
  if (locale.includes("schinese") || locale.startsWith("zh")) return "zh-CN";
  if (locale.includes("brazilian") || locale.startsWith("pt")) return "pt";
  if (locale.includes("latam") || locale.startsWith("es")) return "es";
  if (locale.includes("japanese")) return "ja";
  if (locale.includes("koreana")) return "ko";
  if (locale.includes("german")) return "de";
  if (locale.includes("french")) return "fr";
  if (locale.includes("russian")) return "ru";
  const code = locale.split(/[-_]/, 1)[0];
  return /^[a-z]{2}$/.test(code) ? code : "en";
}

async function imageAsSteamArtwork(url?: string): Promise<{ base64: string; imageType: "jpg" | "png" } | undefined> {
  if (!url) return undefined;
  const response = await fetch(url);
  if (!response.ok) return undefined;
  const blob = await response.blob();
  if (!blob.type.includes("png") && !blob.type.includes("jpeg") && !blob.type.includes("jpg")) {
    const objectUrl = URL.createObjectURL(blob);
    try {
      const image = await loadArtworkImage(objectUrl);
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      canvas.getContext("2d")!.drawImage(image, 0, 0);
      return { base64: canvas.toDataURL("image/png").split(",", 2)[1], imageType: "png" };
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  return { base64: dataUrl.split(",", 2)[1], imageType: blob.type.includes("png") ? "png" : "jpg" };
}

async function imageAsSizedSteamArtwork(
  url: string | undefined,
  width: number,
  height: number,
  mode: "cover" | "contain" = "cover",
  throughBackend = false,
): Promise<{ base64: string; imageType: "png" } | undefined> {
  if (!url) return undefined;
  let objectUrl: string;
  if (throughBackend) {
    const downloaded = await withTimeout(downloadSteamGridDbArtwork(url), 30000);
    objectUrl = `data:${downloaded.mimeType};base64,${downloaded.base64}`;
  } else {
    const response = await fetch(url);
    if (!response.ok) return undefined;
    objectUrl = URL.createObjectURL(await response.blob());
  }
  try {
    const source = await loadArtworkImage(objectUrl);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d")!;
    const scale = mode === "cover"
      ? Math.max(width / source.naturalWidth, height / source.naturalHeight)
      : Math.min(width / source.naturalWidth, height / source.naturalHeight);
    const drawWidth = source.naturalWidth * scale;
    const drawHeight = source.naturalHeight * scale;
    context.drawImage(source, (width - drawWidth) / 2, (height - drawHeight) / 2, drawWidth, drawHeight);
    return { base64: canvas.toDataURL("image/png").split(",", 2)[1], imageType: "png" };
  } finally {
    if (!throughBackend) URL.revokeObjectURL(objectUrl);
  }
}

function loadArtworkImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("artwork image decode failed"));
    image.src = url;
  });
}

async function titleAsSteamLogo(title: string): Promise<{ base64: string; imageType: "png" }> {
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 360;
  const context = canvas.getContext("2d")!;
  context.font = "700 112px Arial, sans-serif";
  context.textBaseline = "middle";
  context.lineJoin = "round";
  const width = Math.min(context.measureText(title).width, 1180);
  const scale = width > 0 ? Math.min(1, 1180 / width) : 1;
  context.translate(50, canvas.height / 2);
  context.scale(scale, scale);
  context.strokeStyle = "rgba(0, 0, 0, .72)";
  context.lineWidth = 14;
  context.strokeText(title, 0, 0);
  context.fillStyle = "#fff";
  context.fillText(title, 0, 0);
  return { base64: canvas.toDataURL("image/png").split(",", 2)[1], imageType: "png" };
}

async function titleAsSteamCapsule(game: SteamLibraryGame): Promise<{ base64: string; imageType: "png" }> {
  const canvas = document.createElement("canvas");
  canvas.width = 600;
  canvas.height = 900;
  const context = canvas.getContext("2d")!;
  const [start, end] = providerCoverColors(game);
  const gradient = context.createLinearGradient(0, 0, canvas.width, canvas.height);
  gradient.addColorStop(0, start);
  gradient.addColorStop(1, end);
  context.fillStyle = gradient;
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "rgba(255,255,255,.16)";
  context.beginPath();
  context.arc(465, 170, 145, 0, Math.PI * 2);
  context.fill();
  context.fillStyle = "rgba(255,255,255,.8)";
  context.font = "500 34px Arial, sans-serif";
  context.fillText(localizeBackend(game.provider_name) ?? game.provider_name, 54, 720);
  context.fillStyle = "#fff";
  context.font = "700 54px Arial, sans-serif";
  const words = game.title.split(/\s+/);
  let line = "";
  let y = 790;
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (context.measureText(candidate).width > 500 && line) {
      context.fillText(line, 54, y);
      y += 62;
      line = word;
    } else line = candidate;
  }
  context.fillText(line, 54, y);
  return { base64: canvas.toDataURL("image/png").split(",", 2)[1], imageType: "png" };
}

async function officialAsSteamCapsule(game: SteamLibraryGame): Promise<{ base64: string; imageType: "png" } | undefined> {
  if (!game.artwork_url) return undefined;
  const response = await fetch(game.artwork_url);
  if (!response.ok) return undefined;
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const source = await loadArtworkImage(objectUrl);
    const canvas = document.createElement("canvas");
    canvas.width = 600;
    canvas.height = 900;
    const context = canvas.getContext("2d")!;
    const coverScale = Math.max(canvas.width / source.naturalWidth, canvas.height / source.naturalHeight);
    const coverWidth = source.naturalWidth * coverScale;
    const coverHeight = source.naturalHeight * coverScale;
    const overflowX = Math.max(0, coverWidth - canvas.width);
    const focalPoint = officialCoverFocalPoint(game) / 100;
    context.drawImage(source, -overflowX * focalPoint, (canvas.height - coverHeight) / 2, coverWidth, coverHeight);
    const shade = context.createLinearGradient(0, 430, 0, canvas.height);
    shade.addColorStop(0, "rgba(7,10,15,0)");
    shade.addColorStop(.62, "rgba(7,10,15,.25)");
    shade.addColorStop(1, "rgba(7,10,15,.96)");
    context.fillStyle = shade;
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#fff";
    context.font = "700 58px Arial, sans-serif";
    context.textBaseline = "bottom";
    context.shadowColor = "rgba(0,0,0,.9)";
    context.shadowBlur = 14;
    context.fillText(game.title, 42, 842, 516);
    return { base64: canvas.toDataURL("image/png").split(",", 2)[1], imageType: "png" };
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function applySteamArtwork(appId: number, game: SteamLibraryGame): Promise<boolean> {
  let requiredWritesSucceeded = true;
  const write = async (name: string, artwork: { base64: string; imageType: "jpg" | "png" } | undefined, type: number) => {
    if (!artwork) {
      requiredWritesSucceeded = false;
      return;
    }
    try {
      await SteamClient.Apps.SetCustomArtworkForApp(appId, artwork.base64, artwork.imageType, type);
    } catch (reason) {
      requiredWritesSucceeded = false;
      console.warn(`[GameBridge] Failed to set Steam ${name}`, reason);
    }
  };
  try {
    const throughBackend = usesSteamGridDbBackend(game);
    // SteamGridDB's CDN and Decky's urllib backend can reset connections when
    // all five large assets are fetched concurrently. Resolve them in order so
    // one transient connection limit cannot discard the complete artwork set.
    const capsule = await imageAsSizedSteamArtwork(
      game.artwork_url, 600, 900, "cover", throughBackend,
    );
    const hero = await imageAsSizedSteamArtwork(
      game.hero_url, 1920, 620, "cover", throughBackend,
    );
    const header = await imageAsSizedSteamArtwork(
      game.header_url, 460, 215, "cover", throughBackend,
    );
    const remoteLogo = await imageAsSizedSteamArtwork(
      game.logo_url, 1280, 360, "contain", throughBackend,
    );
    const icon = await imageAsSizedSteamArtwork(
      game.icon_url, 256, 256, "contain", throughBackend,
    );
    const logo = remoteLogo ?? await titleAsSteamLogo(game.title);
    const capsuleArtwork = game.artwork_source === "official"
      ? await officialAsSteamCapsule(game) ?? capsule ?? await titleAsSteamCapsule(game)
      : capsule ?? await titleAsSteamCapsule(game);
    await write("capsule", capsuleArtwork, STEAM_ARTWORK.Capsule);
    await write("hero", hero, STEAM_ARTWORK.Hero);
    await write("header", header, STEAM_ARTWORK.Header);
    await write("logo", logo, STEAM_ARTWORK.Logo);
    await write("icon", icon, STEAM_ARTWORK.Icon);
    return requiredWritesSucceeded;
  } catch (reason) {
    console.warn("[GameBridge] Failed to set Steam artwork", reason);
    return false;
  }
}

function usesSteamGridDbBackend(game: SteamLibraryGame): boolean {
  if (game.artwork_source === "steamgriddb") return true;
  return [game.artwork_url, game.hero_url, game.header_url, game.logo_url, game.icon_url]
    .some((url) => {
      if (!url) return false;
      try { return STEAMGRIDDB_IMAGE_HOSTS.has(new URL(url).hostname); }
      catch (_) { return false; }
    });
}

function ProviderTabAddon({ count, manageFocus = false }: { count: number; manageFocus?: boolean }) {
  const marker = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!manageFocus) return;
    const node = marker.current;
    if (!node) return;
    let tab: HTMLElement | null = node;
    for (let depth = 0; tab && depth < 7; depth += 1) {
      if (tab.matches('[role="tab"], [tabindex]')) break;
      tab = tab.parentElement;
    }
    const strip = tab?.parentElement;
    if (!tab || !strip) return;
    const previousFlow = strip.getAttribute("flow-children");
    strip.setAttribute("flow-children", "row");
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const tabs = Array.from(strip.children).map((child) => {
        if (!(child instanceof HTMLElement) || child.getClientRects().length === 0) return undefined;
        if (child.matches('[role="tab"], [tabindex]')) return child;
        return child.querySelector<HTMLElement>('[role="tab"], [tabindex]') ?? undefined;
      }).filter((child): child is HTMLElement => Boolean(child));
      const activeElement = node.ownerDocument.activeElement;
      const current = tabs.findIndex((candidate) =>
        candidate === tab && candidate.classList.contains("gpfocus")
        || candidate.contains(activeElement)
        || candidate.classList.contains("gpfocus"),
      );
      if (current < 0) return;
      const step = event.key === "ArrowLeft" ? -1 : 1;
      const next = tabs[current + step];
      if (!next) return;
      event.preventDefault();
      event.stopPropagation();
      next.focus();
    };
    node.ownerDocument.addEventListener("keydown", keydown, true);
    return () => {
      node.ownerDocument.removeEventListener("keydown", keydown, true);
      if (previousFlow === null) strip.removeAttribute("flow-children");
      else strip.setAttribute("flow-children", previousFlow);
    };
  }, [manageFocus]);

  return <span ref={marker}>{count}</span>;
}

function injectProviderTabs(result: unknown): unknown {
  if (!Array.isArray(result)) return result;
  const tuple = Array.isArray(result[0]);
  const tabs = (tuple ? result[0] : result) as any[];
  if (!tabs.some((tab) => tab?.id === "AllGames")) return result;
  const template = tabs.find((tab) => tab?.id === "AllGames");
  const nativeTabs = tabs.filter((tab) => tab?.id !== "gamebridge-epic" && tab?.id !== "gamebridge-mihoyo");
  const epicGames = cachedSteamLibraryGames?.filter((game) => game.provider_id === "epic") ?? [];
  const mihoyoGames = cachedSteamLibraryGames?.filter((game) =>
    game.provider_id === "mihoyo_cn" || game.provider_id === "hoyoplay_global") ?? [];
  const installedProviders = new Set(
    cachedDashboard?.providers
      .filter((provider) => provider.status.state === "installed")
      .map((provider) => provider.id) ?? [],
  );
  const epicConnected = cachedDashboard?.providers.some(
    (provider) => provider.id === "epic" && provider.status.state === "connected",
  ) ?? false;
  const epicTab = {
    ...template,
    title: "EPIC",
    id: "gamebridge-epic",
    footer: { ...(template?.footer ?? {}) },
    content: <ProviderSteamLibraryPage providerIds={["epic"]} />,
    renderTabAddon: () => <ProviderTabAddon count={epicGames.length} manageFocus />,
  };
  const providerTabs = epicConnected ? [epicTab] : [];
  if (mihoyoGames.length || installedProviders.has("mihoyo_cn") || installedProviders.has("hoyoplay_global")) providerTabs.push({
    ...template,
    title: t("mihoyoCn"),
    id: "gamebridge-mihoyo",
    footer: { ...(template?.footer ?? {}) },
    content: <ProviderSteamLibraryPage providerIds={["mihoyo_cn", "hoyoplay_global"]} />,
    renderTabAddon: () => <ProviderTabAddon count={mihoyoGames.length || 4} />,
  });
  const merged = [...nativeTabs, ...providerTabs];
  return tuple ? [merged, ...result.slice(1)] : merged;
}

type ReactHooks = {
  useMemo?: <T>(factory: () => T, dependencies: unknown[]) => T;
  useEffect?: unknown;
};

function getActiveReactHooks(): ReactHooks | undefined {
  const react = (window as any).SP_REACT;
  const legacy = react?.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED
    ?.ReactCurrentDispatcher?.current as ReactHooks | undefined;
  if (legacy?.useMemo) return legacy;
  const clientInternals = react?.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE;
  if (clientInternals) {
    for (const candidate of Object.values(clientInternals) as ReactHooks[]) {
      if (candidate?.useMemo && candidate.useEffect) return candidate;
    }
  }
  return undefined;
}

function applySteamLibraryPatch() {
  const routePatch = routerHook.addPatch("/library", (props: any) => {
    const routeChildren = props?.children;
    if (!routeChildren?.type) return props;
    let innerPatch: { unpatch(): void } | undefined;
    let memoCache: unknown;
    useEffect(() => () => innerPatch?.unpatch(), []);
    afterPatch(routeChildren, "type", (_args, first) => {
      if (!first?.type) return first;
      afterPatch(first, "type", (_innerArgs, second) => {
        const memoComponent = second?.type?.type;
        if (memoCache) {
          second.type = memoCache;
          return second;
        }
        if (typeof memoComponent !== "function") return second;
        wrapReactType(second);
        const wrapped = second.type;
        innerPatch = replacePatch(wrapped, "type", (args) => {
          const hooks = getActiveReactHooks();
          if (!hooks?.useMemo) return memoComponent(...args);
          const originalUseMemo = hooks.useMemo;
          hooks.useMemo = <T,>(factory: () => T, dependencies: unknown[]) =>
            originalUseMemo(() => injectProviderTabs(factory()) as T, dependencies);
          try {
            return memoComponent(...args);
          } finally {
            hooks.useMemo = originalUseMemo;
          }
        });
        memoCache = wrapped;
        return second;
      });
      return first;
    });
    return props;
  });
  return () => {
    routerHook.removePatch("/library", routePatch);
  };
}

function NativeEpicInstallSection({ appId }: { appId: number }) {
  const [game, setGame] = useState<GameDetails | null>();
  const [job, setJob] = useState<InstallJob>();
  const [requiredBytes, setRequiredBytes] = useState<number | null>();
  const [error, setError] = useState<string>();
  const [modifiers, setModifiers] = useState<ModifierAvailability>({ lsfg: false, framegen: false });
  const markerRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    let active = true;
    const refreshDetails = () => {
      const overview = (window as any).appStore?.GetAppOverviewByAppID?.(appId);
      const title = String(overview?.display_name ?? overview?.strDisplayName ?? "") || undefined;
      void getSteamGameDetails(appId, title).then((value) => {
        if (!active) return;
        setGame(value);
        setJob(value?.install_job);
      }).catch((reason) => active && setError(String(reason)));
    };
    refreshDetails();
    // The native Steam route can remain mounted while a GameBridge install or
    // update is started from Quick Access. Poll the lightweight detail record
    // so a newly-created job is discovered without requiring route remounting.
    const timer = window.setInterval(refreshDetails, 1000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [appId]);

  useEffect(() => {
    if (!game) return;
    // Updating Steam artwork while the native details route is visible forces
    // Steam to temporarily unmount its Hero image. Artwork is installed when a
    // shortcut is created or explicitly refreshed; route entry only repairs the
    // stable provider-to-shortcut mapping.
    void registerSteamShortcut(game.provider_id, game.external_game_id, appId)
      .catch((reason) => console.warn("[GameBridge] Native shortcut registration failed", reason));
  }, [appId, game?.id, game?.provider_id, game?.external_game_id]);

  useEffect(() => {
    let mounted = true;
    setRequiredBytes(undefined);
    if (!game || game.provider_id !== "epic" || game.installed) return () => { mounted = false; };
    void getStorageLocations(game.id)
      .then((storage) => {
        if (mounted) setRequiredBytes(storage.required_bytes > 0 ? storage.required_bytes : null);
      })
      .catch(() => {
        if (mounted) setRequiredBytes(null);
      });
    return () => { mounted = false; };
  }, [game?.id, game?.provider_id, game?.installed]);

  useEffect(() => {
    void getLaunchModifierAvailability().then(setModifiers).catch(() => undefined);
  }, [appId]);

  useEffect(() => {
    if (!game) return;
    const steamDocument = markerRef.current?.ownerDocument;
    if (!steamDocument?.body) return;
    const active = Boolean(job && !["completed", "cancelled", "failed_retryable", "failed_permanent"].includes(job.state));
    const isOfficialLauncherGame = game.provider_id === "mihoyo_cn" || game.provider_id === "hoyoplay_global";
    const canUninstall = game.installed || active;
    let currentButton: HTMLElement | undefined;
    let originalLabelText: string | undefined;
    let originalPathData: string | undefined;
    let originalPathFill: string | undefined;
    let statsElement: HTMLElement | undefined;
    let progressElement: HTMLElement | undefined;
    let progressFill: HTMLElement | undefined;
    let progressHost: HTMLElement | undefined;
    let originalHostPosition = "";
    let originalHostOverflow = "";
    let nativeActionStyle: HTMLStyleElement | undefined;
    const nativeActionInlineStyles = new Map<HTMLElement, string>();
    const nativeAccentBackground = (button: HTMLElement) => {
      const candidates = [button, button.parentElement, ...Array.from(button.children)]
        .filter((item): item is HTMLElement => Boolean(item && item.nodeType === 1));
      for (const candidate of candidates) {
        const style = getComputedStyle(candidate);
        if (style.backgroundImage && style.backgroundImage !== "none") return style.backgroundImage;
      }
      for (const candidate of candidates) {
        const color = getComputedStyle(candidate).backgroundColor;
        if (color && color !== "transparent" && color !== "rgba(0, 0, 0, 0)") return color;
      }
      return "currentColor";
    };
    type NativeActionState = "install" | "update" | "downloading" | "paused";
    const nativeAction = (state: NativeActionState) => {
      if (state === "downloading") {
        return {
          label: steamT("#GameAction_Pause", "pauseDownload"),
          path: "M14.5 30.5a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1v-25a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v25Zm15-25a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v25a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-25Z",
        };
      }
      return {
        label: state === "paused"
          ? steamT("#GameAction_Download", "download")
          : state === "update"
            ? steamT("#GameAction_Update", "update")
            : steamT("#GameAction_Install", "install"),
        path: "M29 23V27H7V23H2V32H34V23H29ZM20 14.1716L24.5858 9.58578L27.4142 12.4142L18 21.8284L8.58582 12.4142L11.4142 9.58578L16 14.1715V2H20V14.1716Z",
      };
    };
    const updateNativeActionButton = (button: HTMLElement) => {
      const state: NativeActionState = active && job
        ? (job.state === "paused" ? "paused" : "downloading")
        : game.installed ? "update" : "install";
      const action = nativeAction(state);
      button.classList.toggle(
        "gamebridge-native-install-action",
        game.provider_id === "epic",
      );
      if (game.provider_id === "epic") {
        const layers = [button, ...Array.from(button.querySelectorAll<HTMLElement>("*"))];
        for (const layer of layers) {
          const style = getComputedStyle(layer);
          const hasBackground = layer === button
            || style.backgroundImage !== "none"
            || (style.backgroundColor !== "transparent" && style.backgroundColor !== "rgba(0, 0, 0, 0)");
          if (!hasBackground) continue;
          if (!nativeActionInlineStyles.has(layer)) {
            nativeActionInlineStyles.set(layer, layer.style.cssText);
          }
          layer.style.setProperty("background-color", "#1a9fff", "important");
          layer.style.setProperty("background-image", "none", "important");
          layer.style.setProperty("transition", "none", "important");
          layer.style.setProperty("filter", "none", "important");
          layer.style.setProperty("opacity", "1", "important");
        }
      }
      const svg = button.querySelector<SVGSVGElement>("svg");
      if (svg) {
        svg.setAttribute("viewBox", "0 0 36 36");
        svg.setAttribute("fill", "none");
        const path = svg.querySelector<SVGPathElement>("path");
        if (path) {
          if (originalPathData === undefined) originalPathData = path.getAttribute("d") ?? "";
          if (originalPathFill === undefined) originalPathFill = path.getAttribute("fill") ?? "";
          path.setAttribute("d", action.path);
          path.setAttribute("fill", "currentColor");
        }
      }
      const label = button.querySelector<HTMLElement>("svg + div")
        ?? Array.from(button.querySelectorAll<HTMLElement>("div")).find((node) => !node.querySelector("div"));
      if (label) {
        if (originalLabelText === undefined) originalLabelText = label.textContent ?? "";
        const actionLabel = isOfficialLauncherGame
          ? t("launchOfficialClient", { provider: localizeBackend(game.provider_name) ?? game.provider_name })
          : action.label;
        if (label.textContent !== actionLabel) label.textContent = actionLabel;
      }
    };
    const updateProgress = (button: HTMLElement) => {
      if (!active || !job) {
        progressElement?.remove();
        progressElement = undefined;
        progressFill = undefined;
        return;
      }
      if (!progressElement) {
        progressHost = button.parentElement?.parentElement ?? button.parentElement ?? undefined;
        if (!progressHost) return;
        originalHostPosition = progressHost.style.position;
        originalHostOverflow = progressHost.style.overflow;
        if (getComputedStyle(progressHost).position === "static") progressHost.style.position = "relative";
        progressHost.style.overflow = "visible";
        progressElement = steamDocument.createElement("div");
        progressElement.dataset.gamebridgeDownloadProgress = "true";
        progressElement.style.cssText = "position:absolute;left:0;top:calc(100% + 5px);width:100%;height:5px;border-radius:3px;background:rgba(255,255,255,.14);pointer-events:none;z-index:5;overflow:hidden";
        progressFill = steamDocument.createElement("div");
        progressFill.style.cssText = "height:100%;border-radius:3px;transition:width .25s ease,opacity .2s ease";
        progressFill.style.background = game.provider_id === "epic"
          ? "#1a9fff"
          : nativeAccentBackground(button);
        progressElement.appendChild(progressFill);
        progressHost.appendChild(progressElement);
      }
      if (progressFill) {
        progressFill.style.width = `${Math.max(1, Math.round(job.progress * 100))}%`;
        progressFill.style.opacity = game.provider_id === "epic"
          ? "1"
          : job.state === "paused" ? ".65" : "1";
      }
    };
    const beginInstall = (path: string) => {
      void startGameInstall(game.id, path)
        .then((result) => getInstallJob(result.jobId))
        .then(setJob)
        .catch((reason) => setError(String(reason)));
    };
    const beginUpdate = () => {
      void startGameUpdate(game.id)
        .then((result) => getInstallJob(result.jobId))
        .then(setJob)
        .catch((reason) => setError(String(reason)));
    };
    const activate = (event: Event) => {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      if (isOfficialLauncherGame) {
        void (async () => {
          await prepareHoYoPlayGameRuntime(game.external_game_id);
          const apps = SteamClient.Apps as any;
          if (typeof apps.RunGame !== "function") throw new Error("Steam RunGame API unavailable");
          const runGameId = await waitForShortcutGameId(appId) ?? shortcutRunGameId(appId);
          await apps.RunGame(runGameId, "", -1, 100);
        })().catch((reason) => setError(String(reason)));
      } else if (active && job) {
        const action = job.state === "paused" ? resumeInstall(job.id) : pauseInstall(job.id);
        void action.then(() => getInstallJob(job.id)).then(setJob).catch((reason) => setError(String(reason)));
      } else if (game.installed && game.update_available) {
        beginUpdate();
      } else {
        showModal(<InstallLocationModal gameId={game.id} gameTitle={game.title} artworkUrl={game.artwork_url} onConfirm={beginInstall} />);
      }
    };
    const managesNativeAction = isOfficialLauncherGame
      ? !game.launchable
      : !game.installed || Boolean(game.update_available) || active;
    const attachToNativeButton = () => {
      if (!nativeActionStyle) {
        nativeActionStyle = steamDocument.createElement("style");
        nativeActionStyle.dataset.gamebridgeNativeActionStyle = "true";
        nativeActionStyle.textContent = `
          .gamebridge-native-install-action {
            background: #1a9fff !important;
            filter: none !important;
            opacity: 1 !important;
          }
          .gamebridge-native-install-action:hover,
          .gamebridge-native-install-action:focus,
          .gamebridge-native-install-action.gpfocus {
            background: #1a9fff !important;
            filter: none !important;
            opacity: 1 !important;
          }
          .gamebridge-native-install-action:active {
            background: #1a9fff !important;
            opacity: 1 !important;
          }
        `;
        steamDocument.head.appendChild(nativeActionStyle);
      }
      const candidates = Array.from(steamDocument.querySelectorAll<HTMLElement>('[role="button"],button'))
        .filter((element) => element.querySelector('svg path[d^="M7.5 32.135"]'));
      for (const candidate of Array.from(candidates)) {
        const button = candidate;
        if (button === currentButton) {
          if (managesNativeAction) {
            updateNativeActionButton(button);
            updateProgress(button);
          }
        } else {
          if (currentButton) continue;
          currentButton = button;
          if (managesNativeAction) {
            button.addEventListener("click", activate, true);
            updateNativeActionButton(button);
            updateProgress(button);
          }
        }
        const playButtonGroup = button.parentElement?.parentElement;
        const actionPanel = playButtonGroup?.parentElement;
        const showsEpicStats = game.provider_id === "epic";
        if (showsEpicStats && playButtonGroup && actionPanel) {
          const existingStats = actionPanel.querySelector<HTMLElement>('[data-gamebridge-play-stats="true"]');
          const overview = (window as any).appStore?.GetAppOverviewByAppID?.(appId);
          const nativeMinutes = Number(overview?.minutes_playtime_forever ?? overview?.nPlaytimeForever ?? 0);
          const nativeLastPlayed = Number(overview?.rt_last_time_played ?? overview?.rtLastTimePlayed ?? 0);
          const persistedHistory = game.play_history ?? { playtimeMinutes: 0, lastPlayed: 0 };
          const displayedMinutes = Math.max(nativeMinutes, persistedHistory.playtimeMinutes);
          const displayedLastPlayed = Math.max(nativeLastPlayed, persistedHistory.lastPlayed);
          const stats: [string, string][] = [];
          if (active && job) {
            stats.push([
              job.state === "paused" ? t("downloadPaused") : steamT("#DisplayStatus_Downloading", "downloading"),
              t("completedPercent", { percent: Math.round(job.progress * 100) }),
            ]);
          } else if (!game.installed) {
            stats.push([
              steamT("#AppDetails_SectionTitle_DiskSpaceRequired", "requiredSpace"),
              requiredBytes === undefined ? t("calculating") : requiredBytes === null ? "--" : formatBytes(requiredBytes),
            ]);
          }
          stats.push(
            [t("lastPlayed"), formatHistoryLastPlayed(displayedLastPlayed)],
            [t("playtime"), formatHistoryPlaytime(displayedMinutes)],
          );
          const statsMarkup = nativeStatsMarkup(stats);
          if (existingStats) {
            statsElement = existingStats;
            // Writing identical markup retriggers the subtree observer. Avoid
            // a self-sustaining mutation loop that can pin steamwebhelper.
            if (statsElement.innerHTML !== statsMarkup) {
              statsElement.innerHTML = statsMarkup;
            }
            return;
          }
          const nativeStatContent = Array.from(actionPanel.querySelectorAll<HTMLElement>("div")).find((node) =>
            !playButtonGroup.contains(node)
            && node.children.length === 2
            && Array.from(node.children).every((child) =>
              child.children.length === 0
              && child.getClientRects().length > 0
              && Boolean(child.textContent?.trim())));
          const nativeStatPanel = nativeStatContent?.parentElement;
          const nativeStatWrapper = nativeStatPanel?.parentElement;
          const nativeStatsRow = nativeStatWrapper?.parentElement;
          statsElement = steamDocument.createElement("div");
          statsElement.innerHTML = statsMarkup;
          statsElement.dataset.gamebridgePlayStats = "true";
          if (nativeStatWrapper && nativeStatsRow) {
            nativeStatsRow.style.setProperty("justify-content", "flex-start", "important");
            nativeStatsRow.style.setProperty("gap", "0", "important");
            nativeStatsRow.replaceChildren(statsElement);
          } else {
            playButtonGroup.insertAdjacentElement("afterend", statsElement);
          }
        }
        return;
      }
    };
    attachToNativeButton();
    const observer = new MutationObserver(attachToNativeButton);
    observer.observe(steamDocument.body, { childList: true, subtree: true });
    const statsTimer = window.setInterval(() => {
      attachToNativeButton();
    }, 1000);
    return () => {
      observer.disconnect();
      window.clearInterval(statsTimer);
      nativeActionStyle?.remove();
      nativeActionInlineStyles.forEach((cssText, element) => {
        element.style.cssText = cssText;
      });
      nativeActionInlineStyles.clear();
      if (currentButton) {
        if (managesNativeAction) {
          currentButton.removeEventListener("click", activate, true);
          const path = currentButton.querySelector<SVGPathElement>("svg path");
          if (path && originalPathData !== undefined) {
            path.setAttribute("d", originalPathData);
            if (originalPathFill) path.setAttribute("fill", originalPathFill);
            else path.removeAttribute("fill");
          }
          const label = currentButton.querySelector<HTMLElement>("svg + div")
            ?? Array.from(currentButton.querySelectorAll<HTMLElement>("div")).find((node) => !node.querySelector("div"));
          if (label && originalLabelText !== undefined) label.textContent = originalLabelText;
        }
        currentButton.classList.remove("gamebridge-native-install-action");
      }
      progressElement?.remove();
      if (progressHost) {
        progressHost.style.position = originalHostPosition;
        progressHost.style.overflow = originalHostOverflow;
      }
      statsElement?.remove();
    };
  }, [
    game?.id,
    game?.title,
    game?.provider_id,
    game?.provider_name,
    game?.external_game_id,
    game?.installed,
    game?.launchable,
    game?.update_available,
    game?.play_history?.playtimeMinutes,
    game?.play_history?.lastPlayed,
    requiredBytes,
    job?.id,
    job?.state,
    job?.progress,
    modifiers.lsfg,
    modifiers.framegen,
  ]);

  return <span ref={markerRef} style={{ display: "none" }} data-gamebridge-error={error} />;
}

function InstallLocationModal({ gameId, gameTitle, artworkUrl, onConfirm, closeModal }: { gameId: string; gameTitle: string; artworkUrl?: string; onConfirm: (path: string) => void; closeModal?: () => void }) {
  const [storage, setStorage] = useState<{ required_bytes: number; locations: StorageLocation[]; recommended_id?: string }>();
  const [selectedId, setSelectedId] = useState<string>();
  const [customLocation, setCustomLocation] = useState<StorageLocation>();
  const [storageError, setStorageError] = useState<string>();
  const submitting = useRef(false);
  useEffect(() => {
    let active = true;
    void getStorageLocations(gameId)
      .then((value) => {
        if (!active) return;
        setStorage(value);
        setSelectedId(value.recommended_id);
      })
      .catch((reason) => active && setStorageError(String(reason)));
    return () => { active = false; };
  }, [gameId]);
  const locations = [...(storage?.locations ?? []), ...(customLocation ? [customLocation] : [])];
  const selected = locations.find((item) => item.id === selectedId);
  const confirmLocation = (location: StorageLocation) => {
    if (!location.enough_space || submitting.current) return;
    submitting.current = true;
    onConfirm(location.path);
    closeModal?.();
  };
  const browse = async () => {
    const startingPath = selected?.path ?? "/home/deck/Games/GameBridge/Epic";
    const picked = await openFilePicker(1, startingPath, false, true);
    if (!picked?.path) return;
    setStorageError(undefined);
    try {
      const requirement = await getInstallRequirements(gameId, picked.path);
      const location: StorageLocation = {
        id: `custom:${requirement.path}`,
        name: t("customPath", { path: requirement.path }),
        path: requirement.path,
        free_bytes: requirement.free_bytes,
        enough_space: requirement.enough_space,
        kind: "drive",
      };
      setCustomLocation(location);
      setSelectedId(location.id);
    } catch (reason) {
      setStorageError(String(reason));
    }
  };
  return (
    <ConfirmModal
      strTitle={steamT("#GameAction_Install", "install")}
      strOKButtonText={steamT("#GameAction_Install", "install")}
      strCancelButtonText={steamT("#Button_Cancel", "cancel")}
      bOKDisabled={!selected?.enough_space}
      onOK={() => {
        if (!selected?.enough_space) return;
        onConfirm(selected.path);
        closeModal?.();
      }}
      onCancel={closeModal}
    >
      <div style={{ minWidth: 540 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, minHeight: 72, paddingBottom: 10, borderBottom: "1px solid rgba(255,255,255,.13)" }}>
          {artworkUrl && <img src={artworkUrl} style={{ width: 112, height: 52, borderRadius: 4, objectFit: "cover" }} />}
          <div style={{ flex: 1, fontSize: 18, fontWeight: 400 }}>{gameTitle}</div>
          <div style={{ fontSize: 14, fontWeight: 700 }}>{storage ? formatBytes(storage.required_bytes) : t("calculating")}</div>
        </div>
        {storage && <>
          <div style={{ margin: "16px 0 10px", fontSize: 14, opacity: .72, fontWeight: 700 }}>{steamT("#Installer_ChooseDestinationFolder", "installTo")}</div>
          <div style={{ display: "grid", gap: 4 }}>
            {locations.map((location) => (
              <DialogButton
                key={location.id}
                disabled={!location.enough_space}
                onClick={() => {
                  setSelectedId(location.id);
                  confirmLocation(location);
                }}
                style={{
                  width: "100%", minHeight: 52, height: 52, padding: "0 18px", borderRadius: 0,
                  opacity: location.enough_space ? 1 : .45,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", width: "100%", gap: 14 }}>
                  <span style={{ width: 20, height: 20, display: "grid", placeItems: "center", color: "currentColor" }}>
                    <StorageDeviceIcon external={location.kind !== "internal"} />
                  </span>
                  <span style={{ flex: 1, textAlign: "left", fontSize: 17, fontWeight: 400 }}>
                    {location.kind === "internal"
                      ? steamT("#ContentManagement_InternalStorage", "internalDrive")
                      : location.name}
                  </span>
                  {location.id === storage.recommended_id && <span title={t("recommended")} style={{ fontSize: 16 }}>★</span>}
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{t("spaceAvailable", { size: formatBytes(location.free_bytes) })}</span>
                </div>
              </DialogButton>
            ))}
            <DialogButton onClick={() => void browse()} style={{ width: "100%", minHeight: 52, height: 52, padding: "0 18px", marginTop: 4, borderRadius: 0 }}>
              <div style={{ display: "flex", alignItems: "center", width: "100%", gap: 14 }}>
                <span style={{ width: 20, height: 20, display: "grid", placeItems: "center", fontSize: 19 }}><FaFolderOpen /></span>
                <span style={{ fontSize: 17, fontWeight: 400 }}>{t("chooseOther")}</span>
              </div>
            </DialogButton>
          </div>
        </>}
        {storage && !locations.some((item) => item.enough_space) && <div style={{ color: "#ff6b6b", marginTop: 12 }}>{t("noStorageSpace")}</div>}
        {storageError && <div style={{ color: "#ff6b6b", marginTop: 12 }}>{t("storageReadFailed", { error: localizeBackend(storageError) ?? storageError })}</div>}
      </div>
    </ConfirmModal>
  );
}

function StorageDeviceIcon({ external }: { external: boolean }) {
  return external ? (
    <svg viewBox="0 0 36 36" width="20" height="20" fill="none" aria-hidden="true">
      <path d="M12 4L4 12V32H32V4H12ZM16 16H12V8H16V16ZM22 16H18V8H22V16ZM28 16H24V8H28V16Z" fill="currentColor" />
    </svg>
  ) : (
    <svg viewBox="0 0 36 36" width="20" height="20" fill="none" aria-hidden="true">
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M33 19.9286H3L7.35714 6H28.6429L33 19.9286ZM3 31.7143V24.2143H33V31.7143H3ZM21.0952 26.1826C21.4476 25.9471 21.8619 25.8214 22.2857 25.8214C22.854 25.8214 23.3991 26.0472 23.8009 26.4491C24.2028 26.8509 24.4286 27.396 24.4286 27.9643C24.4286 28.3881 24.3029 28.8024 24.0674 29.1548C23.832 29.5072 23.4973 29.7818 23.1058 29.944C22.7142 30.1062 22.2833 30.1486 21.8677 30.066C21.452 29.9833 21.0702 29.7792 20.7705 29.4795C20.4708 29.1798 20.2667 28.798 20.184 28.3823C20.1013 27.9667 20.1438 27.5358 20.306 27.1442C20.4682 26.7527 20.7428 26.418 21.0952 26.1826ZM28.7143 25.8214C28.2905 25.8214 27.8762 25.9471 27.5238 26.1826C27.1714 26.418 26.8967 26.7527 26.7345 27.1442C26.5724 27.5358 26.5299 27.9667 26.6126 28.3823C26.6953 28.798 26.8994 29.1798 27.1991 29.4795C27.4987 29.7792 27.8806 29.9833 28.2962 30.066C28.7119 30.1486 29.1428 30.1062 29.5343 29.944C29.9259 29.7818 30.2605 29.5072 30.496 29.1548C30.7315 28.8024 30.8571 28.3881 30.8571 27.9643C30.8571 27.396 30.6314 26.8509 30.2295 26.4491C29.8277 26.0472 29.2826 25.8214 28.7143 25.8214Z"
        fill="currentColor"
      />
    </svg>
  );
}

function formatBytes(bytes: number): string {
  const value = bytes >= 1024 ** 3 ? bytes / 1024 ** 3 : bytes / 1024 ** 2;
  const unit = bytes >= 1024 ** 3 ? "GB" : "MB";
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: unit === "GB" ? 1 : 0 }).format(value)} ${unit}`;
}

function nativeStatsMarkup(stats: readonly (readonly [string, string])[]): string {
  const escape = (text: string) => text.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]!);
  const columns = stats.map(([label, value]) => `<div style="display:flex;flex-direction:column;white-space:nowrap"><div data-gamebridge-stat-label="true" style="font-family:'Motiva Sans',Helvetica,sans-serif;font-size:12px;font-weight:700;line-height:22px;letter-spacing:.5px;color:rgba(255,255,255,.7)">${escape(label)}</div><div data-gamebridge-stat-value="true" style="font-family:'Motiva Sans',Helvetica,sans-serif;font-size:16px;font-weight:500;line-height:20px;color:#fff">${escape(value)}</div></div>`).join("");
  return `<div style="display:flex;align-items:flex-start;height:46px;flex:0 0 auto"><div style="display:flex;gap:20px">${columns}</div></div>`;
}

function formatHistoryLastPlayed(timestamp: number): string {
  if (!timestamp) return t("never");
  const played = new Date(timestamp * 1000);
  const today = new Date();
  if (
    played.getFullYear() === today.getFullYear()
    && played.getMonth() === today.getMonth()
    && played.getDate() === today.getDate()
  ) {
    return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(0, "day");
  }
  if (played.getFullYear() === today.getFullYear()) {
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(played);
  }
  return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(played);
}

function formatHistoryPlaytime(minutes: number): string {
  if (!minutes) return "--";
  if (minutes < 60) return t("minutes", { count: minutes });
  return t("hours", { count: Math.round(minutes / 6) / 10 });
}

function applySteamAppDetailsPatch() {
  const renderPatches: { unpatch(): void }[] = [];
  const patchedRenderProps = new WeakSet<object>();
  const routePatch = routerHook.addPatch("/library/app/:appid", (tree: any) => {
    const routeProps = findInReactTree(tree, (node: any) => node?.renderFunc != null) as any;
    if (!routeProps?.renderFunc || patchedRenderProps.has(routeProps)) return tree;
    patchedRenderProps.add(routeProps);
    const handler = createReactTreePatcher([
      (node: any) => findInReactTree(node, (item: any) => item?.props?.children?.props?.overview != null)?.props?.children,
    ], (_args, rendered: any) => {
      const overviewNode = findInReactTree(rendered, (item: any) => item?.props?.children?.props?.overview != null) as any;
      const appId = Number(overviewNode?.props?.children?.props?.overview?.appid);
      if (!Number.isFinite(appId) || appId <= 2_000_000_000) return rendered;
      const inner = findInReactTree(rendered, (item: any) =>
        Array.isArray(item?.props?.children) && typeof item?.props?.className === "string" &&
        Boolean(appDetailsClasses?.InnerContainer) && item.props.className.includes(appDetailsClasses.InnerContainer)) as any;
      const children = inner?.props?.children;
      if (!Array.isArray(children) || children.some((item: any) => item?.key === `gamebridge-install-${appId}`)) return rendered;
      let index = children.findIndex((item: any) => {
          const className = String(item?.props?.className ?? "");
          return Boolean(appDetailsHeaderClasses?.TopCapsule && className.includes(appDetailsHeaderClasses.TopCapsule));
        }) + 1;
      if (index <= 0) index = 1;
      children.splice(index, 0, <NativeEpicInstallSection key={`gamebridge-install-${appId}`} appId={appId} />);
      return rendered;
    });
    renderPatches.push(afterPatch(routeProps, "renderFunc", handler));
    return tree;
  });
  return () => {
    renderPatches.forEach((patch) => patch.unpatch());
    routerHook.removePatch("/library/app/:appid", routePatch);
  };
}

async function withTimeout<T>(promise: Promise<T>, milliseconds = 8000): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(t("backendTimeout"))), milliseconds);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer!);
  }
}

function CleanupConfirmModal({ onConfirm, closeModal }: { onConfirm: (deleteGames: boolean) => void; closeModal?: () => void }) {
  const [deleteGames, setDeleteGames] = useState(false);
  return (
    <ConfirmModal
      strTitle={t("cleanupBeforeUninstall")}
      strDescription={t("cleanupDescription")}
      strOKButtonText={t("cleanupNow")}
      strCancelButtonText={steamT("#Button_Cancel", "cancel")}
      bDestructiveWarning
      onOK={() => {
        closeModal?.();
        onConfirm(deleteGames);
      }}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      <div style={{ marginTop: 10, padding: 10, borderRadius: 8, border: "1px solid rgba(239,68,68,.32)", background: "rgba(239,68,68,.08)" }}>
        <ToggleField
          label={t("cleanupDeleteGamesLabel")}
          description={t("cleanupDeleteGamesDescription")}
          checked={deleteGames}
          onChange={setDeleteGames}
        />
      </div>
    </ConfirmModal>
  );
}

function PlayHistoryImportModal({ backups, onConfirm, closeModal }: { backups: PlayHistoryBackup[]; onConfirm: (path: string) => void; closeModal?: () => void }) {
  const [selected, setSelected] = useState<string>();
  return (
    <ConfirmModal
      strTitle={t("importPlayHistory")}
      strDescription={t("playHistoryChooseBackup")}
      strOKButtonText={t("importPlayHistory")}
      strCancelButtonText={steamT("#Button_Cancel", "cancel")}
      bOKDisabled={!selected}
      onOK={() => {
        if (!selected) return;
        closeModal?.();
        onConfirm(selected);
      }}
      onCancel={closeModal}
      bHideCloseIcon={false}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
        {backups.map((backup) => (
          <DialogButton
            key={backup.path}
            onClick={() => setSelected(backup.path)}
            style={{ textAlign: "left", border: selected === backup.path ? "2px solid #1a9fff" : undefined }}
          >
            <div style={{ fontWeight: 700 }}>{backup.name}</div>
            <div style={{ fontSize: 13, opacity: .72 }}>{t("playHistoryBackupGames", { count: backup.gameCount })}</div>
          </DialogButton>
        ))}
      </div>
    </ConfirmModal>
  );
}

function Content() {
  const [dashboard, setDashboard] = useState<Dashboard | undefined>(() => cachedDashboard);
  const [preparingCompatibility, setPreparingCompatibility] = useState(false);
  const [error, setError] = useState<string>();
  const [operation, setOperation] = useState<{ providerId: string; label: string }>();
  const [cleaningUp, setCleaningUp] = useState(false);
  const [playHistoryBusy, setPlayHistoryBusy] = useState(false);
  const [playHistoryMessage, setPlayHistoryMessage] = useState<string>();
  const [artworkSettings, setArtworkSettings] = useState<ArtworkSettings>();
  const [steamGridDbKey, setSteamGridDbKey] = useState("");
  const [artworkBusy, setArtworkBusy] = useState(false);
  const [artworkMessage, setArtworkMessage] = useState<string>();
  const [mihoyoRegion, setMihoyoRegion] = useState<"mihoyo_cn" | "mihoyo_bilibili" | "hoyoplay_global">("mihoyo_cn");
  const [changingMihoyoChannel, setChangingMihoyoChannel] = useState(false);
  const installLock = useRef(false);
  const automaticCompatibilityAttempted = useRef(false);

  useEffect(() => {
    void getHoYoPlayChannelSelection().then((selection) => {
      setMihoyoRegion(selection.current === "global"
        ? "hoyoplay_global"
        : selection.current === "bilibili" ? "mihoyo_bilibili" : "mihoyo_cn");
    }).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, []);

  const refresh = useCallback(async () => {
    try {
      setError(undefined);
      const nextDashboard = await withTimeout(getDashboard());
      cachedDashboard = nextDashboard;
      setDashboard(nextDashboard);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  const connectEpic = useCallback(async (action?: string) => {
    if (installLock.current) return;
    try {
      installLock.current = true;
      setError(undefined);
      if (action === "install_cli") {
        setOperation({ providerId: "epic", label: t("installingLegendary") });
        await withTimeout(installProviderTool("epic"), 120000);
      }
      setOperation({ providerId: "epic", label: t("validatingEpic") });
      const authentication = automaticEpicLogin();
      Navigation.NavigateToExternalWeb(EPIC_LOGIN_URL);
      await withTimeout(authentication, 360000);
      (Navigation as typeof Navigation & { NavigateBack?: () => void }).NavigateBack?.();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installLock.current = false;
      setOperation(undefined);
    }
  }, [refresh]);

  const logoutEpic = useCallback(async () => {
    if (installLock.current) return;
    try {
      installLock.current = true;
      setError(undefined);
      setOperation({ providerId: "epic", label: t("epicLoggingOut") });
      await withTimeout(logoutProvider("epic"), 60000);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installLock.current = false;
      setOperation(undefined);
    }
  }, [refresh]);

  const confirmEpicLogout = useCallback(() => showModal(
    <ConfirmModal
      strTitle={t("epicLogoutTitle")}
      strDescription={t("epicLogoutDescription")}
      strOKButtonText={t("epicLogout")}
      strCancelButtonText={steamT("#Button_Cancel", "cancel")}
      onOK={() => void logoutEpic()}
    />,
  ), [logoutEpic]);

  const runOfficialLauncherAction = useCallback(async (provider: ProviderSummary) => {
    if (installLock.current) return;
    try {
      installLock.current = true;
      setError(undefined);
      setOperation({ providerId: provider.id, label: provider.status.action === "download_installer" ? t("downloadingOfficialInstaller") : t("launchingOfficialClient") });
      if (provider.status.action === "run_installer") {
        await launchProviderThroughSteam(provider, "installer");
      } else if (provider.status.action === "download_installer") {
        await withTimeout(downloadProviderInstaller(provider.id), 900000);
        await launchProviderThroughSteam(provider, "installer");
      } else {
        await launchProviderThroughSteam(provider, "launcher");
      }
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installLock.current = false;
      setOperation(undefined);
    }
  }, [refresh]);

  const syncAllLibraries = useCallback(async () => {
    if (installLock.current) return;
    const providers = dashboard?.providers.filter((provider) =>
      provider.status.state === "connected" && provider.capabilities.owned_library) ?? [];
    if (providers.length === 0) return;
    try {
      installLock.current = true;
      setError(undefined);
      setOperation({ providerId: "all", label: t("syncingLibrary") });
      for (const provider of providers) {
        await withTimeout(syncProviderLibrary(provider.id), 180000);
      }
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installLock.current = false;
      setOperation(undefined);
    }
  }, [dashboard?.providers, refresh]);

  const validateStatuses = useCallback(async () => {
    if (installLock.current) return;
    try {
      installLock.current = true;
      setError(undefined);
      setOperation({ providerId: "epic", label: t("validatingAccount") });
      await withTimeout(refreshProviderStatus("epic"), 60000);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      installLock.current = false;
      setOperation(undefined);
    }
  }, [refresh]);

  useEffect(() => {
    void refresh();
    void withTimeout(getArtworkSettings()).then((settings) => {
      setArtworkSettings(settings);
      if (settings.steamGridDbConfigured) setArtworkMessage(t("steamGridDbConnected"));
    }).catch(() => undefined);
  }, [refresh]);

  const saveAndTestArtworkKey = useCallback(async () => {
    if (artworkBusy || !steamGridDbKey.trim()) return;
    const candidate = steamGridDbKey.trim();
    try {
      setArtworkBusy(true);
      setArtworkMessage(undefined);
      setSteamGridDbKey("");
      const settings = await withTimeout(saveSteamGridDbKey(candidate), 20000);
      setArtworkSettings(settings);
      setArtworkMessage(t("steamGridDbConnected"));
    } catch (reason) {
      try {
        const settings = await withTimeout(getArtworkSettings());
        setArtworkSettings(settings);
        const verified = settings.steamGridDbConfigured
          && (settings.steamGridDbLastValidationSucceeded
            || (await withTimeout(testSteamGridDbConnection(), 30000)).connected);
        if (verified) {
          setArtworkMessage(t("steamGridDbConnected"));
          return;
        }
      } catch { /* Keep the original failure below. */ }
      const raw = reason instanceof Error ? reason.message : String(reason);
      setArtworkMessage(t("steamGridDbFailed", { error: localizeBackend(raw) ?? (raw || t("steamGridDbUnknownError")) }));
    } finally {
      setArtworkBusy(false);
    }
  }, [artworkBusy, steamGridDbKey]);

  useEffect(() => {
    if (!dashboard || dashboard.runtime.ready || automaticCompatibilityAttempted.current) return;
    automaticCompatibilityAttempted.current = true;
    setPreparingCompatibility(true);
    void withTimeout(prepareCompatibility(), 120000)
      .then((runtime) => {
        setDashboard((current) => current ? { ...current, runtime } : current);
        if (cachedDashboard) cachedDashboard = { ...cachedDashboard, runtime };
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setPreparingCompatibility(false));
  }, [dashboard?.runtime.ready]);

  if (!dashboard && !error) return <Spinner />;

  const setupCompatibility = async () => {
    if (preparingCompatibility) return;
    setPreparingCompatibility(true);
    setError(undefined);
    try {
      const runtime = await withTimeout(prepareCompatibility(), 120000);
      setDashboard((current) => current ? { ...current, runtime } : current);
      if (cachedDashboard) cachedDashboard = { ...cachedDashboard, runtime };
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setPreparingCompatibility(false);
    }
  };

  const runCleanup = async (deleteGames: boolean) => {
    if (cleaningUp) return;
    setCleaningUp(true);
    setError(undefined);
    try {
      const result = await withTimeout(cleanupBeforeUninstall(deleteGames), 900000);
      for (const appId of result.steamAppIds) {
        try {
          await (SteamClient.Apps as any).RemoveShortcut?.(appId);
          localStorage.removeItem(`gamebridge.artwork.${appId}`);
        } catch (_) { /* Steam may already have removed the shortcut. */ }
      }
      cachedDashboard = undefined;
      cachedSteamLibraryGames = undefined;
      await refresh();
      showModal(
        <ConfirmModal
          strTitle={t("cleanupCompleteTitle")}
          strDescription={result.errors.length
            ? t("cleanupCompleteWarning")
            : result.removedGames > 0
              ? t("cleanupCompleteGamesRemoved", { count: result.removedGames })
              : t("cleanupCompleteDescription")}
          strOKButtonText={steamT("#Button_OK", "continue")}
        />,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCleaningUp(false);
    }
  };

  const confirmCleanup = () => showModal(
    <CleanupConfirmModal onConfirm={(deleteGames) => void runCleanup(deleteGames)} />,
  );

  const exportManagedPlayHistory = async () => {
    if (playHistoryBusy) return;
    setPlayHistoryBusy(true);
    setPlayHistoryMessage(undefined);
    try {
      const games = await withTimeout(getSteamLibraryGames(), 30000);
      const runtime = games.flatMap((game): PlayHistoryRecord[] => {
        if (!game.steam_app_id) return [];
        const overview = (window as any).appStore?.GetAppOverviewByAppID?.(game.steam_app_id);
        return [{
          steamAppId: game.steam_app_id,
          playtimeMinutes: Number(overview?.minutes_playtime_forever ?? overview?.nPlaytimeForever ?? 0),
          lastPlayed: Number(overview?.rt_last_time_played ?? overview?.rtLastTimePlayed ?? 0),
        }];
      });
      const result = await withTimeout(exportPlayHistory(runtime), 30000);
      setPlayHistoryMessage(t("playHistoryExported", { count: result.count, path: result.path }));
    } catch (reason) {
      setPlayHistoryMessage(t("playHistoryFailed", { error: String(reason) }));
    } finally {
      setPlayHistoryBusy(false);
    }
  };

  const applyManagedPlayHistory = async (sourcePath: string) => {
    if (playHistoryBusy) return;
    setPlayHistoryBusy(true);
    setPlayHistoryMessage(t("working"));
    try {
      const games = await withTimeout(getSteamLibraryGames(), 30000);
      const runtime = games.flatMap((game): PlayHistoryRecord[] => {
        if (!game.steam_app_id) return [];
        const overview = (window as any).appStore?.GetAppOverviewByAppID?.(game.steam_app_id);
        return [{
          steamAppId: game.steam_app_id,
          playtimeMinutes: Number(overview?.minutes_playtime_forever ?? overview?.nPlaytimeForever ?? 0),
          lastPlayed: Number(overview?.rt_last_time_played ?? overview?.rtLastTimePlayed ?? 0),
        }];
      });
      const result = await withTimeout(importPlayHistory(sourcePath, runtime), 30000);
      setPlayHistoryMessage(t(result.nonEmpty > 0 ? "playHistoryImported" : "playHistoryImportedEmpty", {
        matched: result.matched, updated: result.updated,
      }));
      if (result.restartRequired && result.nonEmpty > 0) {
        window.setTimeout(() => (SteamClient.User as any).StartRestart(false), 250);
      }
    } catch (reason) {
      setPlayHistoryMessage(t("playHistoryFailed", { error: String(reason) }));
    } finally {
      setPlayHistoryBusy(false);
    }
  };

  const importManagedPlayHistory = async () => {
    if (playHistoryBusy) return;
    setPlayHistoryBusy(true);
    setPlayHistoryMessage(t("working"));
    try {
      const backups = await withTimeout(playHistoryExports(), 10000);
      if (!backups.length) throw new Error(t("playHistoryNoBackups"));
      setPlayHistoryMessage(undefined);
      showModal(<PlayHistoryImportModal backups={backups} onConfirm={(path) => void applyManagedPlayHistory(path)} />);
    } catch (reason) {
      setPlayHistoryMessage(t("playHistoryFailed", { error: String(reason) }));
    } finally {
      setPlayHistoryBusy(false);
    }
  };

  const epicProvider = dashboard?.providers.find((provider) => provider.id === "epic");
  const mihoyoProviders = dashboard?.providers.filter((provider) =>
    provider.id === "mihoyo_cn" || provider.id === "hoyoplay_global"
  ) ?? [];
  const selectedMihoyoProvider = mihoyoProviders.find((provider) =>
    provider.id === (mihoyoRegion === "mihoyo_bilibili" ? "mihoyo_cn" : mihoyoRegion))
    ?? mihoyoProviders[0];
  const canSyncLibraries = dashboard?.providers.some((provider) =>
    provider.status.state === "connected" && provider.capabilities.owned_library) ?? false;

  return (
    <Focusable flow-children="vertical" style={{ width: "100%", maxWidth: "100%", overflowX: "hidden" }}>
      <style>{`
        .gamebridge-action { transition: filter 120ms ease, transform 120ms ease, box-shadow 120ms ease; }
        .gamebridge-action:hover:not([aria-disabled="true"]):not(:disabled) { filter: brightness(1.12); }
        .gamebridge-action:focus, .gamebridge-action:focus-visible, .gamebridge-action.gpfocus,
        .gamebridge-action:has(:focus-visible) {
          outline: 2px solid #fff !important; outline-offset: 2px;
          box-shadow: 0 0 0 4px rgba(26,159,255,.48) !important;
          filter: brightness(1.16); transform: translateY(-1px);
        }
        .gamebridge-action:active:not([aria-disabled="true"]):not(:disabled) { transform: scale(.985); filter: brightness(.94); }
        .gamebridge-action[aria-disabled="true"], .gamebridge-action:disabled { opacity: .52; filter: saturate(.55); }
        .gamebridge-action > span, .gamebridge-action-label { white-space: nowrap !important; }
        .gamebridge-input:focus, .gamebridge-input:focus-within {
          outline: 2px solid #fff !important; outline-offset: 2px;
          box-shadow: 0 0 0 4px rgba(26,159,255,.42) !important;
        }
      `}</style>
      <PanelSection title={t("platforms")}>
        {error && <PanelSectionRow><div style={{ color: "#ff8080", width: "100%" }}>{t("error", { error: localizeBackend(error) ?? error })}</div></PanelSectionRow>}
        {dashboard?.providers.filter((provider) =>
          provider.id !== "mihoyo_cn" && provider.id !== "hoyoplay_global"
        ).map((provider) => (
          <PanelSectionRow key={provider.id}>
            <div style={{ ...DASHBOARD_CARD_STYLE, marginBottom: 8 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {provider.id === "epic" && <EpicGamesLogo />}
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 16, fontWeight: 650 }}>{localizeBackend(provider.name) ?? provider.name}</div>
                  {provider.status.account && <div style={{ marginTop: 2, fontSize: 12, opacity: .55, overflow: "hidden", textOverflow: "ellipsis" }}>{provider.status.account}</div>}
                </div>
                {provider.id === "epic" ? (
                  <DialogButton
                    className={DASHBOARD_ACTION_CLASS}
                    onClick={provider.status.state === "connected" ? confirmEpicLogout : () => void connectEpic(provider.status.action)}
                    disabled={operation?.providerId === "epic"}
                    aria-label={provider.status.state === "connected" ? t("epicLogout") : t("connectAndSync")}
                    style={{
                      ...DASHBOARD_STATUS_BADGE_STYLE,
                      width: "auto", minWidth: 0, height: 28, margin: 0,
                      color: provider.status.state === "connected" ? "#8ff0b5" : "rgba(255,255,255,.82)",
                      background: provider.status.state === "connected" ? "rgba(45,190,105,.16)" : "rgba(255,255,255,.08)",
                      border: provider.status.state === "connected" ? "1px solid rgba(80,220,140,.18)" : "1px solid rgba(255,255,255,.08)",
                    }}
                  >
                    {provider.status.state === "connected"
                      ? (localizeBackend(provider.status.message) ?? t("connected"))
                      : t("epicLoginRequired")}
                  </DialogButton>
                ) : (
                  <div style={{
                    ...DASHBOARD_STATUS_BADGE_STYLE,
                    color: "rgba(255,255,255,.72)",
                    background: "rgba(255,255,255,.08)",
                  }}>
                    {localizeBackend(provider.status.message) ?? provider.status.state}
                  </div>
                )}
              </div>
            </div>
          </PanelSectionRow>
        ))}
        {selectedMihoyoProvider && (
          <PanelSectionRow>
            <div style={DASHBOARD_CARD_STYLE}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <MiHoYoLogo />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 16, fontWeight: 650 }}>{t("mihoyoPlatform")}</div>
                </div>
                <Focusable flow-children="horizontal" style={{ ...DASHBOARD_SEGMENTED_CONTAINER_STYLE, gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
                  <div aria-hidden style={{
                    ...DASHBOARD_SEGMENTED_SLIDER_STYLE,
                    width: "calc(33.333% - 2px)",
                    transform: mihoyoRegion === "hoyoplay_global" ? "translateX(200%)" : mihoyoRegion === "mihoyo_bilibili" ? "translateX(100%)" : "translateX(0)",
                  }} />
                  {(["mihoyo_cn", "mihoyo_bilibili", "hoyoplay_global"] as const).map((providerId) => (
                    <DialogButton
                      key={providerId}
                      className={DASHBOARD_ACTION_CLASS}
                      aria-pressed={mihoyoRegion === providerId}
                      style={DASHBOARD_SEGMENTED_BUTTON_STYLE}
                      disabled={changingMihoyoChannel}
                      onClick={() => {
                        setChangingMihoyoChannel(true);
                        setError(undefined);
                        void switchHoYoPlayChannelSelection(
                          providerId === "hoyoplay_global"
                            ? "global"
                            : providerId === "mihoyo_bilibili" ? "bilibili" : "official",
                        ).then((selection) => {
                          setMihoyoRegion(selection.current === "global"
                            ? "hoyoplay_global"
                            : selection.current === "bilibili" ? "mihoyo_bilibili" : "mihoyo_cn");
                        }).catch((reason) => {
                          setError(reason instanceof Error ? reason.message : String(reason));
                        }).finally(() => setChangingMihoyoChannel(false));
                      }}
                    >
                      {providerId === "mihoyo_cn" ? t("chinaRegion") : providerId === "mihoyo_bilibili" ? t("bilibiliChannel") : t("globalRegion")}
                    </DialogButton>
                  ))}
                </Focusable>
              </div>
              {!operation && (
                <DialogButton className={DASHBOARD_ACTION_CLASS} style={{ ...DASHBOARD_PRIMARY_BUTTON_STYLE, marginTop: 10 }} onClick={() => void runOfficialLauncherAction(selectedMihoyoProvider)}>
                  <span className="gamebridge-action-label" style={{ width: "100%", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7 }}>
                    <FaPlay size={11} />
                    {selectedMihoyoProvider.status.action === "launch_client"
                      ? t("launchMihoyo")
                      : selectedMihoyoProvider.status.action === "run_installer"
                        ? t("installOfficialProvider", { provider: localizeBackend(selectedMihoyoProvider.name) ?? selectedMihoyoProvider.name })
                        : t("downloadAndInstallOfficial", { provider: localizeBackend(selectedMihoyoProvider.name) ?? selectedMihoyoProvider.name })}
                  </span>
                </DialogButton>
              )}
              {operation?.providerId === selectedMihoyoProvider.id && <OperationProgress label={operation.label} />}
            </div>
          </PanelSectionRow>
        )}
        {operation && operation.providerId !== selectedMihoyoProvider?.id && <PanelSectionRow><OperationProgress label={operation.label} /></PanelSectionRow>}
        {!operation && (
          <PanelSectionRow>
            <Focusable flow-children="horizontal" style={{
              width: "100%", display: "grid",
              gridTemplateColumns: canSyncLibraries ? "repeat(2, minmax(0, 1fr))" : "1fr",
              gap: 8, marginTop: 4, padding: "4px 0 0",
            }}>
              {canSyncLibraries && (
                <DialogButton className={DASHBOARD_ACTION_CLASS} style={{ ...DASHBOARD_SECONDARY_BUTTON_STYLE, fontSize: 13 }} onClick={() => void syncAllLibraries()}>
                  <span className="gamebridge-action-label" style={{ width: "100%", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, fontSize: 13 }}>
                  <span aria-hidden style={{ width: DASHBOARD_ICON_SIZE, height: DASHBOARD_ICON_SIZE, flex: `0 0 ${DASHBOARD_ICON_SIZE}px`, display: "grid", placeItems: "center" }}><LuRefreshCw size={15} strokeWidth={2.2} /></span>
                    <span style={{ lineHeight: `${DASHBOARD_ICON_SIZE}px`, display: "inline-flex", alignItems: "center" }}>{t("syncEpicLibrary")}</span>
                  </span>
                </DialogButton>
              )}
              <DialogButton className={DASHBOARD_ACTION_CLASS} style={{ ...DASHBOARD_SECONDARY_BUTTON_STYLE, fontSize: 13 }} onClick={() => void validateStatuses()}>
                <span className="gamebridge-action-label" style={{ width: "100%", display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, fontSize: 13 }}>
                  <span aria-hidden style={{ width: DASHBOARD_ICON_SIZE, height: DASHBOARD_ICON_SIZE, flex: `0 0 ${DASHBOARD_ICON_SIZE}px`, display: "grid", placeItems: "center" }}><LuRefreshCw size={15} strokeWidth={2.2} /></span>
                  <span style={{ lineHeight: `${DASHBOARD_ICON_SIZE}px`, display: "inline-flex", alignItems: "center" }}>{t("refreshStatus")}</span>
                </span>
              </DialogButton>
            </Focusable>
          </PanelSectionRow>
        )}
      </PanelSection>
      {!dashboard?.runtime.ready && (
        <PanelSection title={t("compatibilityRuntime")}>
          <PanelSectionRow>
            <div style={{ width: "100%", display: "flex", justifyContent: "space-between" }}>
              <span>{t("umuRuntime")}</span>
              <span style={{ opacity: .7 }}>{preparingCompatibility ? t("preparingCompatibility") : t("runtimeNotReady")}</span>
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <DialogButton className={DASHBOARD_ACTION_CLASS} style={DASHBOARD_SECONDARY_BUTTON_STYLE} disabled={preparingCompatibility} onClick={() => void setupCompatibility()}>
              {preparingCompatibility ? t("preparingCompatibility") : t("prepareCompatibility")}
            </DialogButton>
          </PanelSectionRow>
        </PanelSection>
      )}
      <PanelSection title={t("artworkMetadata")}>
        <PanelSectionRow>
            <div style={DASHBOARD_CARD_STYLE}>
            <div style={{ color: "#fff", fontSize: 18, fontWeight: 700, marginBottom: 9 }}>{t("steamGridDbArtwork")}</div>
            <div style={{
              display: "inline-block",
              marginBottom: 12,
              ...DASHBOARD_STATUS_BADGE_STYLE,
              color: artworkSettings?.steamGridDbConfigured ? "#8ff0b5" : "rgba(255,255,255,.72)",
              background: artworkSettings?.steamGridDbConfigured ? "rgba(45,190,105,.16)" : "rgba(255,255,255,.08)",
            }}>
              {artworkSettings?.steamGridDbConfigured ? t("apiConfigured") : t("apiNotConfigured")}
            </div>
            <DialogButton className={DASHBOARD_ACTION_CLASS} style={DASHBOARD_PRIMARY_BUTTON_STYLE} onClick={() => Navigation.NavigateToExternalWeb(STEAMGRIDDB_STEAM_LOGIN_URL)}>
              <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, whiteSpace: "nowrap" }}>
                <FaArrowUpRightFromSquare size={14} />
                {t("loginSteamGridDb")}
              </span>
            </DialogButton>
            <DialogButton className={DASHBOARD_ACTION_CLASS} style={{ ...DASHBOARD_SECONDARY_BUTTON_STYLE, marginTop: 8 }} onClick={() => Navigation.NavigateToExternalWeb(STEAMGRIDDB_API_URL)}>
              <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, whiteSpace: "nowrap" }}>
                <FaArrowUpRightFromSquare size={14} />
                {t("openSteamGridDbApi")}
              </span>
            </DialogButton>
            <div style={{ margin: "9px 0 12px", fontSize: 12, opacity: .62 }}>{t("steamGridDbLoginHint")}</div>
            <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 116px", alignItems: "center", gap: 8, margin: "0 12px" }}>
              <TextField
                className="gamebridge-input"
                value={steamGridDbKey}
                bIsPassword
                aria-label={t("apiKeyPlaceholder")}
                {...{ placeholder: t("apiKeyPlaceholder") }}
                style={{
                  width: "100%", minWidth: 0, height: 44, boxSizing: "border-box", fontSize: 14, fontStyle: "normal",
                  color: "rgba(255,255,255,.82)", background: "#111820", border: "1px solid rgba(255,255,255,.16)", borderRadius: 8,
                }}
                onChange={(event) => setSteamGridDbKey(event.currentTarget.value)}
              />
              <DialogButton
                className={DASHBOARD_ACTION_CLASS}
                style={{
                  ...DASHBOARD_SECONDARY_BUTTON_STYLE, width: 116, minWidth: 116, maxWidth: 116,
                  color: "rgba(255,255,255,.82)", background: "rgba(255,255,255,.12)", border: "1px solid rgba(255,255,255,.08)", whiteSpace: "nowrap",
                }}
                aria-disabled={artworkBusy || !steamGridDbKey.trim()}
                onClick={() => void saveAndTestArtworkKey()}
              >
                {artworkBusy ? t("working") : t("saveAndVerify")}
              </DialogButton>
            </Focusable>
            {artworkMessage && <div style={{ marginTop: 10, fontSize: 12, opacity: .66 }}>{artworkMessage}</div>}
          </div>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title={t("maintenance")}>
        <PanelSectionRow>
          <div style={{ width: "100%" }}>
            <div data-gamebridge-maintenance-card="true" style={DASHBOARD_CARD_STYLE}>
            <div style={{ color: "#fff", fontSize: 18, fontWeight: 700, marginBottom: 9 }}>{t("playHistoryBackup")}</div>
            <div style={{ fontSize: 13, opacity: .7, lineHeight: 1.5, marginBottom: 12 }}>{t("playHistoryHint")}</div>
            <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 10 }}>
              <DialogButton className={DASHBOARD_ACTION_CLASS} style={DASHBOARD_SECONDARY_BUTTON_STYLE} disabled={playHistoryBusy} onClick={() => void exportManagedPlayHistory()}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, whiteSpace: "nowrap" }}><LuDownload size={DASHBOARD_ICON_SIZE} />{t("exportPlayHistory")}</span>
              </DialogButton>
              <DialogButton className={DASHBOARD_ACTION_CLASS} style={DASHBOARD_SECONDARY_BUTTON_STYLE} disabled={playHistoryBusy} onClick={() => void importManagedPlayHistory()}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7, whiteSpace: "nowrap" }}><LuUpload size={DASHBOARD_ICON_SIZE} />{t("importPlayHistory")}</span>
              </DialogButton>
            </Focusable>
            {playHistoryMessage && <div style={{ marginBottom: 12, fontSize: 12, opacity: .7, overflowWrap: "anywhere" }}>{playHistoryMessage}</div>}
            <div style={{ paddingTop: 10, marginTop: 2, borderTop: "1px solid rgba(255,255,255,.08)" }}>
            <div style={{ fontSize: 13, opacity: .7, lineHeight: 1.5, marginBottom: 10 }}>{t("cleanupHint")}</div>
            <DialogButton className={DASHBOARD_ACTION_CLASS} style={DASHBOARD_SECONDARY_BUTTON_STYLE} disabled={cleaningUp} onClick={confirmCleanup}>
              {cleaningUp ? t("cleaningUp") : t("cleanupBeforeUninstall")}
            </DialogButton>
            </div>
            </div>
            <div
              data-gamebridge-author-credit="true"
              style={{ marginTop: 14, paddingBottom: 2, textAlign: "center", fontSize: 12, color: "rgba(255,255,255,.46)", userSelect: "none" }}
            >
              {t("authorCredit", { author: "邪能皮卡丘" })}
            </div>
          </div>
        </PanelSectionRow>
      </PanelSection>
    </Focusable>
  );
}

function LibraryView({ onBack }: { onBack: () => void }) {
  const pageSize = 20;
  const [page, setPage] = useState<GamePage>();
  const [items, setItems] = useState<GameItem[]>([]);
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [reloadToken, setReloadToken] = useState(0);
  const [error, setError] = useState<string>();
  const [selectedGameId, setSelectedGameId] = useState<string>();

  const load = useCallback(async () => {
    try {
      setError(undefined);
      const nextPage = await withTimeout(listGames(query, offset, pageSize));
      setPage(nextPage);
      setItems((current) => offset === 0 ? nextPage.items : [...current, ...nextPage.items]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [query, offset, reloadToken]);

  useEffect(() => { void load(); }, [load]);

  const search = () => {
    setItems([]);
    setPage(undefined);
    setOffset(0);
    setQuery(queryInput.trim());
    setReloadToken((value) => value + 1);
  };

  if (selectedGameId) {
    return <GameDetailView gameId={selectedGameId} onBack={() => setSelectedGameId(undefined)} />;
  }

  return (
    <Focusable flow-children="vertical" style={{ width: "100%", maxWidth: "100%", overflowX: "hidden" }}>
      <PanelSection title={t("gameLibrary")}>
        <PanelSectionRow><ButtonItem onClick={onBack}>{t("backOverview")}</ButtonItem></PanelSectionRow>
        <PanelSectionRow>
          <div style={{ width: "100%" }}>
            <TextField
              label={t("searchGames")}
              value={queryInput}
              bShowClearAction
              onChange={(event) => setQueryInput(event.currentTarget.value)}
            />
            <ButtonItem onClick={search}>{steamT("#GameList_Search", "search")}</ButtonItem>
          </div>
        </PanelSectionRow>
        {error && <PanelSectionRow><div style={{ color: "#ff8080" }}>{t("error", { error: localizeBackend(error) ?? error })}</div></PanelSectionRow>}
        {!page && !error && <PanelSectionRow><OperationProgress label={t("readingLibrary")} /></PanelSectionRow>}
        {items.map((game) => (
          <PanelSectionRow key={`${game.provider_id}:${game.external_game_id}`}>
            <Focusable
              onActivate={() => setSelectedGameId(game.id)}
              onClick={() => setSelectedGameId(game.id)}
              style={{ width: "100%", padding: "10px 8px", borderRadius: 8 }}
            >
              <div style={{ fontSize: 16, fontWeight: 600 }}>{game.title}</div>
              <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4, fontSize: 12, opacity: .65 }}>
                <span>{localizeBackend(game.provider_name) ?? game.provider_name}</span>
                <span>{compatibilityLabel(game.compatibility_status)}</span>
              </div>
            </Focusable>
          </PanelSectionRow>
        ))}
        {page && items.length === 0 && <PanelSectionRow>{t("noGames")}</PanelSectionRow>}
      </PanelSection>
      {page && (
        <PanelSection title={t("shownCount", { shown: items.length, total: page.total })}>
          <PanelSectionRow>
            {items.length < page.total
              ? <ButtonItem onClick={() => setOffset(items.length)}>{t("loadMore")}</ButtonItem>
              : <div style={{ width: "100%", textAlign: "center", opacity: .55 }}>{t("allGamesLoaded")}</div>}
          </PanelSectionRow>
        </PanelSection>
      )}
    </Focusable>
  );
}

function GameDetailView({ gameId, onBack }: { gameId: string; onBack: () => void }) {
  const [game, setGame] = useState<GameDetails>();
  const [error, setError] = useState<string>();
  const [confirmInstall, setConfirmInstall] = useState(false);
  const [job, setJob] = useState<InstallJob>();
  const [openingOfficialClient, setOpeningOfficialClient] = useState(false);
  const [changingChannel, setChangingChannel] = useState(false);
  const refreshRevision = useRef(0);

  const refreshGame = useCallback(async () => {
    const revision = ++refreshRevision.current;
    try {
      const value = await withTimeout(getGameDetails(gameId));
      if (revision !== refreshRevision.current) return;
      setGame(value);
      setJob(value.install_job);
      setError(undefined);
    } catch (reason) {
      if (revision === refreshRevision.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  }, [gameId]);

  useEffect(() => {
    void refreshGame();
    return () => { refreshRevision.current += 1; };
  }, [refreshGame]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshGame();
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refreshGame]);

  useEffect(() => {
    if (!job || ["completed", "cancelled", "failed_retryable", "failed_permanent"].includes(job.state)) return;
    const timer = setInterval(() => {
      void getInstallJob(job.id).then(setJob).catch((reason) => {
        setError(reason instanceof Error ? reason.message : String(reason));
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [job]);

  const beginInstall = async () => {
    try {
      setError(undefined);
      const result = await startGameInstall(gameId);
      setConfirmInstall(false);
      setJob(await getInstallJob(result.jobId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const openOfficialClient = async () => {
    if (!game || openingOfficialClient) return;
    setOpeningOfficialClient(true);
    setError(undefined);
    const provider: ProviderSummary = {
      id: game.provider_id,
      name: game.provider_name,
      capabilities: {},
      status: {
        state: game.official_client_installed ? "installed" : "not_installed",
      },
    };
    try {
      if (game.official_client_installed) {
        await launchProviderThroughSteam(provider, "launcher");
      } else {
        await withTimeout(downloadProviderInstaller(game.provider_id), 900000);
        await launchProviderThroughSteam(provider, "installer");
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setOpeningOfficialClient(false);
    }
  };

  const isOfficialLauncherGame = game?.provider_id === "mihoyo_cn"
    || game?.provider_id === "hoyoplay_global";
  const providerLabel = game ? localizeBackend(game.provider_name) ?? game.provider_name : "";

  return (
    <Focusable flow-children="vertical" style={{ width: "100%", maxWidth: "100%", overflowX: "hidden" }}>
      <PanelSection title={t("gameDetails")}>
        <PanelSectionRow><ButtonItem onClick={onBack}>{t("backLibrary")}</ButtonItem></PanelSectionRow>
        {!game && !error && <PanelSectionRow><OperationProgress label={t("readingGame")} /></PanelSectionRow>}
        {error && <PanelSectionRow><div style={{ color: "#ff8080" }}>{t("error", { error: localizeBackend(error) ?? error })}</div></PanelSectionRow>}
        {game?.artwork_url && (
          <PanelSectionRow>
            <img
              src={game.artwork_url}
              alt={game.title}
              style={{ display: "block", width: "100%", maxHeight: 260, objectFit: "cover", borderRadius: 10 }}
            />
          </PanelSectionRow>
        )}
        {game && (
          <>
            <PanelSectionRow>
              <div style={{ width: "100%" }}>
                <div style={{ fontSize: 20, fontWeight: 700 }}>{game.title}</div>
                <div style={{ marginTop: 4, opacity: .7 }}>{game.developer ?? providerLabel}</div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow><DetailLine label={t("compatibility")} value={compatibilityLabel(game.compatibility_status)} /></PanelSectionRow>
            <PanelSectionRow><DetailLine label={t("installStatus")} value={game.installed ? steamT("#DisplayStatus_NotLaunchable", "installed") : t("notInstalled")} /></PanelSectionRow>
            {game.description && <PanelSectionRow><div style={{ lineHeight: 1.45, opacity: .8 }}>{game.description}</div></PanelSectionRow>}
            {job && <PanelSectionRow><InstallProgress job={job} /></PanelSectionRow>}
            {game.provider_id === "mihoyo_cn" && game.channel_profile
              && (game.installed || game.external_game_id !== "bh3_cn") && (
              <PanelSectionRow>
                <ChannelProfileControl
                  profile={game.channel_profile}
                  disabled={changingChannel}
                  onSwitch={async (channel) => {
                    setChangingChannel(true);
                    setError(undefined);
                    try {
                      await switchHoYoPlayChannelProfile(gameId, channel);
                      await refreshGame();
                    } catch (reason) {
                      setError(reason instanceof Error ? reason.message : String(reason));
                    } finally {
                      setChangingChannel(false);
                    }
                  }}
                />
              </PanelSectionRow>
            )}
            {isOfficialLauncherGame && (
              <PanelSectionRow>
                <ButtonItem disabled={openingOfficialClient} onClick={() => void openOfficialClient()}>
                  {openingOfficialClient
                    ? t("launchingOfficialClient")
                    : game.official_client_installed
                      ? t("launchOfficialClient", { provider: providerLabel })
                      : t("downloadAndInstallOfficial", { provider: providerLabel })}
                </ButtonItem>
              </PanelSectionRow>
            )}
            {!isOfficialLauncherGame && !job && !confirmInstall && !game.installed && (
              <PanelSectionRow><ButtonItem onClick={() => setConfirmInstall(true)}>{t("installDefault")}</ButtonItem></PanelSectionRow>
            )}
            {!isOfficialLauncherGame && confirmInstall && (
              <PanelSectionRow>
                <div style={{ width: "100%" }}>
                  <div style={{ marginBottom: 8, lineHeight: 1.4 }}>
                    {t("defaultInstallConfirm")}
                  </div>
                  <ButtonItem onClick={() => void beginInstall()}>{t("confirmInstall")}</ButtonItem>
                  <ButtonItem onClick={() => setConfirmInstall(false)}>{steamT("#Button_Cancel", "cancel")}</ButtonItem>
                </div>
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>
    </Focusable>
  );
}

function ChannelProfileControl({ profile, disabled, onSwitch }: {
  profile: ChannelProfile;
  disabled: boolean;
  onSwitch: (channel: "official" | "bilibili") => Promise<void>;
}) {
  const channels = ["official", "bilibili"] as const;
  return (
    <div style={{ width: "100%", padding: 10, boxSizing: "border-box", borderRadius: 10, background: "rgba(0,0,0,.22)", border: "1px solid rgba(255,255,255,.08)" }}>
      <div style={{ marginBottom: 8, fontSize: 13, color: "rgba(255,255,255,.72)" }}>{t("gameChannel")}</div>
      <Focusable flow-children="horizontal" style={DASHBOARD_SEGMENTED_CONTAINER_STYLE}>
        {profile.current !== "unknown" && <div style={{
          ...DASHBOARD_SEGMENTED_SLIDER_STYLE,
          width: "calc(50% - 2px)",
          transform: profile.current === "bilibili" ? "translateX(100%)" : "translateX(0)",
        }} />}
        {channels.map((channel) => {
          return <DialogButton
            key={channel}
            disabled={disabled}
            onClick={() => void onSwitch(channel)}
            style={{ position: "relative", minWidth: 0, height: 38, padding: "0 8px", borderRadius: 6, background: "transparent", color: profile.current === channel ? "#fff" : "rgba(255,255,255,.68)", fontSize: 13, whiteSpace: "nowrap" }}
          >{channel === "official" ? t("officialChannel") : t("bilibiliChannel")}</DialogButton>;
        })}
      </Focusable>
      <div style={{ marginTop: 8, fontSize: 12, lineHeight: 1.4, color: "rgba(255,255,255,.58)" }}>
        {t(profile.mode === "qr" ? "channelQrHint" : "channelProfileHint")}
      </div>
    </div>
  );
}

function InstallProgress({ job }: { job: InstallJob }) {
  const percent = Math.max(0, Math.min(100, job.progress * 100));
  const terminalLabels: Record<string, string> = {
    completed: t("installComplete"), cancelled: t("cancelled"), failed_retryable: t("installFailedRetry"),
    failed_permanent: t("installFailed")
  };
  const label = terminalLabels[job.state] ?? localizeBackend(job.payload.phase) ?? t("preparing");
  if (job.state === "completed") {
    return <div style={{ width: "100%", padding: "10px 12px", borderRadius: 8, background: "rgba(70,180,90,.16)", color: "#7ee08d" }}>
      ✓ {t("installComplete")}
    </div>;
  }
  return <div style={{ width: "100%" }}>
    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
      <span>{label}</span><span>{percent.toFixed(1)}%</span>
    </div>
    <div style={{ width: "100%", height: 9, borderRadius: 5, overflow: "hidden", background: "rgba(255,255,255,.16)" }}>
      <div style={{ width: `${percent}%`, height: "100%", background: "#1a9fff", transition: "width .3s ease" }} />
    </div>
    <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6, fontSize: 12, opacity: .65 }}>
      <span>{new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(job.payload.downloadedMiB ?? 0)} MiB · {new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(job.payload.speedMiBs ?? 0)} MiB/s</span>
      <span>{t("remaining", { eta: job.payload.eta ?? "--:--:--" })}</span>
    </div>
  </div>;
}

function DetailLine({ label, value }: { label: string; value: string }) {
  return <div style={{ display: "flex", justifyContent: "space-between", gap: 12, width: "100%" }}>
    <span style={{ opacity: .65 }}>{label}</span><span style={{ textAlign: "right", overflowWrap: "anywhere" }}>{value}</span>
  </div>;
}

function compatibilityLabel(status: string): string {
  const labels: Record<string, string> = {
    verified: t("verified"), playable: t("playable"), experimental: t("experimental"),
    blocked: t("blocked"), unknown: t("unknown"), deprecated: t("deprecated")
  };
  return labels[status] ?? status;
}

function OperationProgress({ label }: { label: string }) {
  return (
    <div style={{ width: "100%", marginTop: 10, overflow: "hidden" }}>
      <style>{`@keyframes gamebridge-progress { from { transform: translateX(-110%); } to { transform: translateX(340%); } }`}</style>
      <div style={{ marginBottom: 6, fontSize: 13, opacity: .85 }}>{label}</div>
      <div style={{ width: "100%", height: 8, borderRadius: 4, overflow: "hidden", background: "rgba(255,255,255,.16)" }}>
        <div style={{ width: "30%", height: "100%", borderRadius: 4, background: "#1a9fff", animation: "gamebridge-progress 1.25s ease-in-out infinite" }} />
      </div>
    </div>
  );
}

function EpicGamesLogo() {
  return (
    <div style={{
      width: 40, height: 44, flex: "0 0 auto", display: "grid", placeItems: "center",
    }}>
      <img src={EPIC_GAMES_LOGO} alt="" draggable={false} style={{ display: "block", width: 40, height: 40, objectFit: "contain" }} />
    </div>
  );
}

function MiHoYoLogo() {
  return (
    <div style={{
      width: 40, height: 44, flex: "0 0 auto", display: "grid", placeItems: "center",
    }}>
      <img src={MIHOYO_LAUNCHER_LOGO} alt="" draggable={false} style={{ display: "block", width: 40, height: 40, objectFit: "contain", borderRadius: 9 }} />
    </div>
  );
}

export default definePlugin(() => {
  void getDashboard().then((value) => { cachedDashboard = value; }).catch(() => undefined);
  void getLaunchModifierAvailability().then((value) => {
    cachedModifierAvailability = value;
  }).catch(() => undefined);
  void withTimeout(getSteamLibraryGames(), 60000).then((value) => {
    cachedSteamLibraryGames = value;
    reconcileDirectShortcutTargets(value);
  }).catch(() => undefined);
  const removeLibraryPatch = applySteamLibraryPatch();
  const removeAppDetailsPatch = applySteamAppDetailsPatch();
  const removeManagementMenuPatch = applySteamManagementMenuPatch();
  return {
    name: "GameBridge",
    titleView: <div className={staticClasses.Title}>GameBridge</div>,
    content: <Content />,
    icon: <FaBridge />,
    onDismount: () => {
      removeManagementMenuPatch();
      removeAppDetailsPatch();
      removeLibraryPatch();
    },
  };
});
