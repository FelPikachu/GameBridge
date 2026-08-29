from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / "src/index.tsx").read_text(
    encoding="utf-8"
)


def test_mihoyo_library_uses_one_combined_tab() -> None:
    assert 'id: "gamebridge-hoyoplay"' not in SOURCE
    assert 'providerIds={["mihoyo_cn", "hoyoplay_global"]}' in SOURCE
    assert 'provider.provider_id === "mihoyo_cn" || game.provider_id === "hoyoplay_global"' not in SOURCE


def test_controller_focus_flows_match_visual_layout() -> None:
    assert 'flow-children="vertical"' in SOURCE
    assert SOURCE.count('flow-children="horizontal"') >= 4
    assert 'strip.setAttribute("flow-children", "row")' in SOURCE
    assert 'node.ownerDocument.addEventListener("keydown", keydown, true)' in SOURCE
    assert 'candidate.classList.contains("gpfocus")' in SOURCE
    assert '<ProviderTabAddon count={epicGames.length} providerId="epic" manageFocus />' in SOURCE
    assert 'flow-children="grid"' in SOURCE
    assert 'data-gamebridge-library-card="true"' in SOURCE
    assert 'onGamepadDirection={handleGridGamepadDirection}' in SOURCE
    assert '9: "up", 10: "down", 11: "left", 12: "right"' in SOURCE
    assert "if (!next && direction === \"up\")" in SOURCE
    assert "'[role=\"tab\"][aria-selected=\"true\"]'" in SOURCE


def test_management_entries_do_not_use_delayed_dom_clones() -> None:
    assert "presetItem.cloneNode" not in SOURCE
    assert "data-gamebridge-repair-shortcut" not in SOURCE
    assert "const scheduleMenuRepair = () =>" not in SOURCE
    assert "const menuObserver = new MutationObserver(() =>" not in SOURCE
    assert "}, [game, job, playtime, modifiers.lsfg, modifiers.framegen]);" not in SOURCE
    assert "game?.external_game_id," in SOURCE
    assert "job?.state," in SOURCE


def test_management_presets_are_injected_into_steam_react_menu() -> None:
    assert "function applySteamManagementMenuPatch()" in SOURCE
    assert 'item?.key === "RemoveShortcut"' in SOURCE
    assert 'key="gamebridge-management-default"' in SOURCE
    assert 'key="gamebridge-management-lsfg"' in SOURCE
    assert 'key="gamebridge-management-framegen"' in SOURCE
    assert 'key="gamebridge-management-combined"' in SOURCE
    assert "applyIntegratedManagementItems(items, activeAppId)" in SOURCE
    assert "const removeManagementMenuPatch = applySteamManagementMenuPatch()" in SOURCE
    assert "removeManagementMenuPatch();" in SOURCE
    assert "} else if (game.installed) {" in SOURCE
    assert "menuItems.splice(removeIndex, 1)" in SOURCE
    assert "if (game.installed && cachedModifierAvailability.lsfg)" in SOURCE
    assert "if (game.installed && cachedModifierAvailability.framegen)" in SOURCE
    assert "game.installed && cachedModifierAvailability.lsfg && cachedModifierAvailability.framegen" in SOURCE
    assert "menuItems.splice(insertionIndex, 0, ...entries)" in SOURCE


def test_hoyoplay_management_hides_native_remove_card_and_adds_launch_presets() -> None:
    assert 'const isOfficialLauncherGame = game.provider_id === "mihoyo_cn"' in SOURCE
    assert "let insertionIndex = removeIndex" in SOURCE
    official_branch = SOURCE.split("if (isOfficialLauncherGame) {", 1)[1].split(
        "} else if (game.installed)", 1
    )[0]
    assert "menuItems.splice(removeIndex, 1)" in official_branch
    assert "confirmIntegratedRemoveShortcut" not in SOURCE
    assert "if (!game || game.provider_id === \"mihoyo_cn\"" not in SOURCE


def test_hoyoplay_uninstall_opens_the_selected_official_launcher() -> None:
    assert 'key="gamebridge-management-official-uninstall"' in SOURCE
    assert "scheduleOfficialLauncherToUninstall(game)" in SOURCE
    assert 'if (selection.current === "global") providerId = "hoyoplay_global"' in SOURCE
    assert 'await launchProviderThroughSteam(provider, "launcher")' in SOURCE
    assert "window.setTimeout(() =>" in SOURCE
    assert "openOfficialLauncherToUninstall(game).catch" in SOURCE
    assert 'callable<[providerId: string], { state: string }>("launch_provider_client")' not in SOURCE


def test_hoyoplay_presets_preserve_the_stable_shortcut_route() -> None:
    assert 'if (isHoYoPlayGame && game.steam_shortcut)' in SOURCE
    assert "applyManagedShortcutTarget(appId, game)" in SOURCE
    assert "const launchOptions = await shortcutProfileLaunchPreset(" in SOURCE
    assert "game.steam_shortcut.launch_options," in SOURCE
    assert "game.steam_shortcut.mode," in SOURCE
    assert 'callable<[preset: LaunchPreset, base: string, mode: SteamShortcutProfile["mode"]], string>("shortcut_profile_launch_preset")' in SOURCE
    assert 'onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "default")}' in SOURCE
    assert 'onSelected={() => scheduleIntegratedLaunchPreset(appId, game, "combined")}' in SOURCE
    assert 'applyIntegratedLaunchPreset(appId, game, preset).catch' in SOURCE
    assert 'apps.SetShortcutLaunchOptions(appId, value)' in SOURCE
    shortcut_writer = SOURCE.split("function setShortcutLaunchOptions", 1)[1].split(
        "function managedShortcutForApp", 1
    )[0]
    assert shortcut_writer.index("SetShortcutLaunchOptions") < shortcut_writer.index(
        "SetAppLaunchOptions"
    )


def test_hoyoplay_region_switch_reconciles_the_shared_steam_cards() -> None:
    region_switch = SOURCE.split("void switchHoYoPlayChannelSelection(", 1)[1].split(
        ").catch((reason) =>", 1
    )[0]
    assert "return withTimeout(getSteamLibraryGames(), 60000)" in region_switch
    assert "cacheSteamLibraryGames(games)" in region_switch


def test_epic_install_action_uses_blue_native_style() -> None:
    assert 'game.provider_id === "epic"' in SOURCE
    assert ".gamebridge-native-install-action" in SOURCE
    assert "background: #1a9fff !important" in SOURCE
    assert '? "#1a9fff"' in SOURCE
    assert "filter: none !important" in SOURCE
    assert 'game.provider_id === "epic"\n          ? "1"' in SOURCE


def test_native_details_render_gamebridge_history_as_one_group() -> None:
    assert "SteamClient.Apps.GetPlaytime(appId)" not in SOURCE
    assert 'const showsEpicStats = game.provider_id === "epic"' in SOURCE
    assert 'if (showsEpicStats && playButtonGroup && actionPanel)' in SOURCE
    assert 'const persistedHistory = game.play_history ?? { playtimeMinutes: 0, lastPlayed: 0 }' in SOURCE
    assert 'const displayedMinutes = Math.max(nativeMinutes, persistedHistory.playtimeMinutes)' in SOURCE
    assert 'const displayedLastPlayed = Math.max(nativeLastPlayed, persistedHistory.lastPlayed)' in SOURCE
    assert '[t("lastPlayed"), formatHistoryLastPlayed(displayedLastPlayed)]' in SOURCE
    assert '[t("playtime"), formatHistoryPlaytime(displayedMinutes)]' in SOURCE
    assert 'const statsTimer = window.setInterval' in SOURCE
    assert 'statsElement?.remove();\n      statsElement = undefined;' not in SOURCE
    assert 'if (statsElement.innerHTML !== statsMarkup)' in SOURCE
    assert 'statsElement.innerHTML = statsMarkup' in SOURCE
    assert 'nativeStatsRow.replaceChildren(statsElement)' in SOURCE
    assert 'nativeStatWrapper.replaceWith(statsElement)' not in SOURCE
    assert 'nativeStatsRow.style.setProperty("justify-content", "flex-start", "important")' in SOURCE
    current_branch = SOURCE.split('if (button === currentButton)', 1)[1].split('} else {', 1)[0]
    assert "return;" not in current_branch


def test_uninstalled_epic_details_show_required_space() -> None:
    assert "const [requiredBytes, setRequiredBytes] = useState<number | null>();" in SOURCE
    assert 'game.provider_id !== "epic" || game.installed' in SOURCE
    assert "getStorageLocations(game.id)" in SOURCE
    assert "setRequiredBytes(storage.required_bytes > 0 ? storage.required_bytes : null)" in SOURCE
    assert 'steamT("#AppDetails_SectionTitle_DiskSpaceRequired", "requiredSpace")' in SOURCE
    assert 'requiredBytes === null ? "--" : formatBytes(requiredBytes)' in SOURCE
    assert 'const nativeStatContent = Array.from(actionPanel.querySelectorAll<HTMLElement>("div")).find' in SOURCE
    assert "node.children.length === 2" in SOURCE
    assert "Array.from(node.children).every" in SOURCE
    assert "const nativeStatPanel = nativeStatContent?.parentElement" in SOURCE
    assert "const nativeStatWrapper = nativeStatPanel?.parentElement" in SOURCE
    assert "const nativeStatsRow = nativeStatWrapper?.parentElement" in SOURCE
    assert 'statsElement.style.transform' not in SOURCE
    assert 'data-gamebridge-stat-label="true"' in SOURCE
    assert 'data-gamebridge-stat-value="true"' in SOURCE
    assert 'height:46px;flex:0 0 auto' in SOURCE
    assert '[t("lastPlayed"), formatHistoryLastPlayed(displayedLastPlayed)]' in SOURCE


def test_play_history_import_uses_manual_backup_picker() -> None:
    assert "const backups = await withTimeout(playHistoryExports(), 10000)" in SOURCE
    assert "<PlayHistoryImportModal backups={backups}" in SOURCE
    assert "onConfirm={(path) => void applyManagedPlayHistory(path)}" in SOURCE
    assert "importPlayHistory(sourcePath, runtime)" in SOURCE
    assert "StartRestart(false)" in SOURCE
    assert '[t("playtime"), formatHistoryPlaytime(displayedMinutes)]' in SOURCE
    assert 'display:flex;gap:20px' in SOURCE
    assert "font-family:system-ui" not in SOURCE


def test_last_played_uses_steam_style_date_precision() -> None:
    assert 'new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(0, "day")' in SOURCE
    assert 'played.getFullYear() === today.getFullYear()' in SOURCE
    assert '{ month: "short", day: "numeric" }' in SOURCE
    assert '{ year: "numeric", month: "short", day: "numeric" }' in SOURCE


def test_connected_epic_badge_is_a_focusable_logout_action() -> None:
    assert 'const logoutProvider = callable' in SOURCE
    assert 'await withTimeout(logoutProvider("epic"), 60000)' in SOURCE
    assert 'strTitle={t("epicLogoutTitle")}' in SOURCE
    assert 'provider.id === "epic" ? (' in SOURCE
    assert 'provider.status.state === "connected" ? confirmEpicLogout : () => void connectEpic(provider.status.action)' in SOURCE
    assert 'provider.status.state === "connected" ? t("epicLogout") : t("connectAndSync")' in SOURCE
    assert 'epicProvider?.status.state === "disconnected" && !operation' not in SOURCE


def test_epic_card_installs_legendary_then_opens_login() -> None:
    install = SOURCE.index('await withTimeout(installProviderTool("epic"), 120000)')
    authenticate = SOURCE.index("const authentication = automaticEpicLogin()", install)
    navigate = SOURCE.index("Navigation.NavigateToExternalWeb(EPIC_LOGIN_URL)", authenticate)
    assert install < authenticate < navigate
    assert 'if (action === "install_cli")' in SOURCE
    assert 'setOperation({ providerId: "epic", label: t("installingLegendary") })' in SOURCE
    assert 'epicProvider?.status.action === "install_cli" && (' not in SOURCE
    assert 'onClick={() => void installTool("epic")}' not in SOURCE


def test_dashboard_shows_static_author_credit() -> None:
    assert 'data-gamebridge-author-credit="true"' in SOURCE
    assert 't("authorCredit", { author: "邪能皮卡丘" })' in SOURCE


def test_cleanup_defaults_to_preserving_installed_games() -> None:
    assert "function CleanupConfirmModal" in SOURCE
    assert "const [deleteGames, setDeleteGames] = useState(false)" in SOURCE
    assert 'label={t("cleanupDeleteGamesLabel")}' in SOURCE
    assert 'description={t("cleanupDeleteGamesDescription")}' in SOURCE
    assert "closeModal?: () => void" in SOURCE
    assert "closeModal?.();\n        onConfirm(deleteGames);" in SOURCE
    assert "onCancel={closeModal}" in SOURCE
    assert "cleanupBeforeUninstall(deleteGames)" in SOURCE


def test_native_details_entry_does_not_rewrite_visible_steam_artwork() -> None:
    section = SOURCE[SOURCE.index("function NativeEpicInstallSection"):SOURCE.index("function InstallLocationModal")]
    assert "registerSteamShortcut(game.provider_id, game.external_game_id, appId)" in section
    assert "applySteamArtwork(appId, game)" not in section
    assert "installSteamShortcutArtwork(" not in section
    assert "SetCustomArtworkForApp" not in section


def test_epic_install_action_paints_the_native_background_layer_blue() -> None:
    section = SOURCE[SOURCE.index("function NativeEpicInstallSection"):SOURCE.index("function InstallLocationModal")]
    assert 'layer.style.setProperty("background-color", "#1a9fff", "important")' in section
    assert 'layer.style.setProperty("transition", "none", "important")' in section
    assert "nativeActionInlineStyles.clear()" in section


def test_dashboard_does_not_show_the_legacy_library_entry() -> None:
    dashboard = SOURCE[SOURCE.index("function Content"):SOURCE.index("function LibraryView")]
    assert 't("openGameLibrary")' not in dashboard
    assert 'setView("library")' not in dashboard
    assert "function LibraryView" in SOURCE


def test_compatibility_preparation_is_automatic_and_allows_runtime_download() -> None:
    assert "automaticCompatibilityAttempted.current = true" not in SOURCE
    assert SOURCE.count("withTimeout(prepareCompatibility(), 900000)") == 1
    assert 'callable<[], ToolDownloadProgress>("tool_download_progress")' in SOURCE
    assert "compatPhaseDownloading" in SOURCE
    assert "compatSourceChina" in SOURCE
    assert "runtimeProgress?.progress" in SOURCE
    assert "toolProgressLabel(runtimeProgress)" in SOURCE
    assert 'runtimeProgress?.source === "china"' in SOURCE
    assert "progressElement.append(progressTrack, progressHeader, progressSource)" in SOURCE


def test_beta5_managed_components_use_routed_progress_in_native_order() -> None:
    assert "automaticCompatibilityAttempted.current = true" not in SOURCE
    assert 'callable<[], ToolDownloadProgress>("tool_download_progress")' in SOURCE
    assert "progressElement.append(progressTrack, progressHeader, progressSource)" in SOURCE
    progress = SOURCE[SOURCE.index("function OperationProgress"):]
    track = progress.index('height: 8, borderRadius: 4')
    status = progress.index('marginTop: 6, fontSize: 13')
    source = progress.index("{progress?.source &&")
    assert track < status < source


def test_ready_hoyoplay_native_action_uses_steam_play_label_and_icon() -> None:
    section = SOURCE[SOURCE.index("const updateNativeActionButton"):SOURCE.index("const updateProgress")]
    assert 'steamT("#GameAction_Play", "play")' in section
    assert "if (!isOfficialLauncherGame)" in section
    assert 't("launchOfficialClient"' not in section


def test_hoyoplay_runtime_is_ready_before_first_shortcut_profile_is_read() -> None:
    section = SOURCE.split("const openNativeDetails", 1)[1].split(
        "const moveGameGridFocus", 1
    )[0]
    prepare = section.index("await prepareHoYoPlayGameRuntime(game.external_game_id)")
    refresh = section.index("const fresh = await getGameDetails(game.id)")
    create = section.index("SteamClient.Apps.AddShortcut")
    assert prepare < refresh < create


def test_first_custom_compat_tool_install_prompts_for_one_restart() -> None:
    assert "GetAvailableCompatTools(appId)" in SOURCE
    assert "user.StartRestart(false)" in SOURCE
    assert 'strTitle={t("compatRestartTitle")}' in SOURCE
    assert 'strOKButtonText={t("restartSteam")}' in SOURCE
    assert "showCompatToolRestartPromptIfRequired(existingAppId, game)" in SOURCE
    assert "showCompatToolRestartPromptIfRequired(appId, game)" in SOURCE
    assert "resumeAfterCompatToolRestart" not in SOURCE
    assert "pendingCompatRestart" not in SOURCE


def test_bh3_hotfix_install_is_claimed_and_continued_by_steam() -> None:
    assert "RegisterForShowInstallWizard" in SOURCE
    assert "claimSteamInstallRequest(PROTON_HOTFIX_APP_ID)" in SOURCE
    assert "await installs.SetCreateShortcuts(false, false)" in SOURCE
    assert "await installs.ContinueInstall()" in SOURCE
    assert "await downloads.SetQueueIndex(PROTON_HOTFIX_APP_ID, 0)" in SOURCE
    assert "await downloads.ResumeAppUpdate(PROTON_HOTFIX_APP_ID)" in SOURCE
    assert "removeManagedSteamInstalls();" in SOURCE


def test_maintenance_actions_share_the_dashboard_card_style() -> None:
    assert 'data-gamebridge-maintenance-card="true" style={DASHBOARD_CARD_STYLE}' in SOURCE
    assert 'borderTop: "1px solid rgba(255,255,255,.08)"' in SOURCE
    assert 'data-gamebridge-author-credit="true"' in SOURCE


def test_steamgriddb_login_uses_a_direct_openid_step_before_api_page() -> None:
    assert 'const STEAMGRIDDB_STEAM_LOGIN_URL = "https://steamcommunity.com/openid/login?' in SOURCE
    assert "Navigation.NavigateToExternalWeb(STEAMGRIDDB_STEAM_LOGIN_URL)" in SOURCE
    assert 't("loginSteamGridDb")' in SOURCE
    assert "Navigation.NavigateToExternalWeb(STEAMGRIDDB_API_URL)" in SOURCE
    assert 't("openSteamGridDbApi")' in SOURCE


def test_epic_library_tab_requires_a_connected_account() -> None:
    assert 'provider.id === "epic" && provider.status.state === "connected"' in SOURCE
    assert "const providerTabs = epicConnected ? [epicTab] : [];" in SOURCE
    assert 'tab?.id !== "gamebridge-epic" && tab?.id !== "gamebridge-mihoyo"' in SOURCE


def test_epic_sync_refreshes_the_library_tab_cache_and_count() -> None:
    assert 'gameCount?: number;' in SOURCE
    assert 'providerId="epic"' in SOURCE
    assert 'const GAMEBRIDGE_LIBRARY_UPDATED = "gamebridge-library-updated"' in SOURCE
    assert 'const GAMEBRIDGE_DASHBOARD_UPDATED = "gamebridge-dashboard-updated"' in SOURCE
    assert "await refreshSteamLibraryGameCache();" in SOURCE
    assert "window.dispatchEvent(new Event(GAMEBRIDGE_LIBRARY_UPDATED))" in SOURCE
    assert "window.addEventListener(GAMEBRIDGE_DASHBOARD_UPDATED, update)" in SOURCE
