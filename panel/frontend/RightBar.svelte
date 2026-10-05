<script lang="ts">
	// Lives entirely outside llama.cpp's own source tree — see ../README.md.
	// Reads its data from a small local backend (panel/backend/server.py) that
	// does its own MCP handshake against each server, rather than trusting the
	// bridge's static "configured" flag.
	//
	// Styling and collapse behavior copied 1:1 from SidebarNavigation.svelte
	// (the left aside), mirrored to the right: same floating rounded glass
	// panel — rounded-2xl, bg-muted/60, backdrop-blur-xl, shadow-md — same
	// collapsed-to-w-12-icon-strip / expand-on-click interaction, same
	// transition. Left uses Logo+PanelLeftClose/PanelLeftOpen; this uses
	// PanelRightOpen/PanelRightClose since there is no logo to show collapsed.
	//
	// The two icons come in as props rather than an `@lucide/svelte` import
	// here: this file lives outside tools/ui (see ../README.md), so a bare
	// package specifier resolves node_modules relative to ITS OWN path, never
	// reaching tools/ui/node_modules. A path alias for it "fixes" that but is
	// a *global* Vite alias — it broke @lucide/svelte's subpath icon imports
	// (e.g. @lucide/svelte/icons/chevron-down) for every other component in
	// the app. Importing in +layout.svelte, which already imports from
	// @lucide/svelte and sits inside tools/ui, and passing the two icons down
	// avoids that blast radius entirely.
	import type { Component } from 'svelte';
	// $app/navigation is a SvelteKit virtual module, resolved the same way for
	// any importer regardless of location — unlike @lucide/svelte (see above)
	// it is not a bare node_modules package, so it needs no alias workaround.
	import { goto } from '$app/navigation';
	import { ActionIcon } from '$lib/components/app';
	// Dedicated brand mark (llama.cpp integrations PNG, llama.cpp/assets/) —
	// used everywhere the left sidebar shows Logo, mirrored here. Not
	// misc/Logo: that renders the generic llama mark, not this set's own.
	import IntegrationsMark from './IntegrationsMark.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
		import { ScrollArea } from '$lib/components/ui/scroll-area';
	import { Switch } from '$lib/components/ui/switch';
	import { TooltipSide } from '$lib/enums';
	// Always-mounted side effects (per-chat permission modes) and the Projects
	// UI: both panel-owned, see ./chat-permission/ and ./projects/.
	import ChatPermissionSync from './chat-permission/ChatPermissionSync.svelte';
	import DashboardOnNewChat from './dashboard/DashboardOnNewChat.svelte';
	import { INCOGNITO_SUMMARY, startIncognitoChat } from './incognito';
	import ProjectsPanel from './projects/ProjectsPanel.svelte';
	import { toast, EyeOff } from '$lib/utils/panel-command-runtime';
	import { incognitoChatStore } from '$lib/stores/incognito-chat.svelte';
	import { onDestroy, onMount } from 'svelte';

	interface Props {
		iconOpen: Component;
		iconClose: Component;
	}

	let { iconOpen, iconClose }: Props = $props();

	/** Mirrors the left aside's logo-hover trick: collapsed, the toggle strip
	 *  shows the logo mark itself; hovering reveals the expand arrow. */
	let logoHovered = $state(false);

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const DASHBOARD_URL = `${PANEL_ORIGIN}/api/dashboard`;
	const USAGE_SUMMARY_URL = `${PANEL_ORIGIN}/api/usage-summary`;
	const BACKGROUND_TASKS_URL = `${PANEL_ORIGIN}/api/background-tasks`;
	// Live OpenCode Go plan allowance (rolling 5h / weekly / monthly), proxied
	// by the panel backend so the key never reaches the browser.
	const OPENCODE_PLAN_URL = `${PANEL_ORIGIN}/api/opencode/plan-usage`;
	// Starts/stops the systemd *user* unit behind the image server. Separate
	// from /api/dashboard's reachability probe: this reports the unit's own
	// state (so "stopped" is distinguishable from "starting"), and the POST
	// side is the only thing in the panel that changes the machine's state
	// rather than reading it.
	const SD_SERVER_URL = `${PANEL_ORIGIN}/api/sd-server`;
	const SD_CONTROL_URL = `${SD_SERVER_URL}/control`;
	// How long to keep polling after a start/stop before giving up on seeing
	// the transition settle. Starting sd-server mmaps ~7 GB of weights, which
	// off a cold page cache is not instant; the switch shows "starting…" for
	// as long as it takes rather than flipping on optimistically.
	const SD_SETTLE_TIMEOUT_MS = 120_000;
	const SD_SETTLE_POLL_MS = 2000;
	const POLL_MS = 4000;

	type ProviderUsage =
		| { kind: 'spend'; cost: number; requests: number; tokens: number; pinned: number }
		| {
				kind: 'requests';
				count_5h: number;
				count_24h: number;
				count_total: number;
				total_window: string;
				limit_known: boolean;
				pinned: number;
		  };

	type UsageSummary = {
		providers: Record<string, ProviderUsage | undefined>;
		openrouter_account: { total_credits: number; total_usage: number } | null;
	};

	type Dashboard = {
		llama_server: { reachable: boolean; loaded?: string[] };
		sd_server: { reachable: boolean; model?: string | null };
	};

	/**
	 * One relay model, folded from the panel backend's own journal lines (see
	 * /api/background-tasks).
	 *
	 * `turns` is how many upstream requests were sent, `rejections` how many
	 * of them were refused and repaired. Only `rejections` decides `looping`:
	 * an earlier version inferred a loop from a growing message count, which
	 * flagged ordinary multi-turn agentic chats as stuck — a conversation adds
	 * messages every turn, so the two are indistinguishable by count alone.
	 */
	type RelayTask = {
		kind: 'relay';
		provider: string;
		model: string;
		messages: number;
		first_messages: number;
		turns: number;
		rejections: number;
		last_rejection: string | null;
		tools: number | null;
		grew_by: number;
		looping: boolean;
		rejected: boolean;
	};

	type ModelLoadTask = {
		kind: 'model_load';
		model?: string | null;
		state: string;
		progress?: number | null;
	};

	type BackgroundTasks = {
		tasks: (RelayTask | ModelLoadTask)[];
		relay: RelayTask[];
		model_loads: ModelLoadTask[];
		context_limits: Record<string, number>;
		stale_after_seconds: number;
	};

	/**
	 * State of the image server's systemd unit, as /api/sd-server reports it.
	 * `available: false` means this machine has no such unit (a different
	 * distro, or a machine where the panel runs without systemd) — the switch
	 * is then shown disabled with that as the reason, rather than as an error.
	 */
	type SdServerState = {
		unit?: string;
		active: boolean;
		available: boolean;
		enabled?: boolean;
		state?: string;
		sub_state?: string;
		server?: { reachable: boolean; model?: string | null };
		error?: string;
	};

	let data = $state<Dashboard | null>(null);
	let usage = $state<UsageSummary | null>(null);
	let sdServer = $state<SdServerState | null>(null);
	// Background task list, polled on the same cadence as the dashboard. Kept
	// as a separate state object rather than folded into `data`: it fails
	// independently (journalctl absent, journald rotated), and a null here
	// should hide its section without marking the whole panel unreachable.
	let tasks = $state<BackgroundTasks | null>(null);
	// True from the moment the switch is flipped until the unit settles into
	// the requested state (or the wait times out): drives the spinner and
	// blocks a second flip mid-transition.
	let sdBusy = $state(false);

	// Writing a key here goes straight to disk from panel/backend/server.py —
	// it never passes through the local model's own context the way asking it
	// in chat to write the file would. The value is submit-once: cleared from
	// the input immediately after a successful save, and never re-fetched
	// (the status endpoint returns only whether each key is set, not its value).
	// One item per remote provider: its page, the usage line beside it, and
	// the secret key belonging to it (saved straight to disk through the panel
	// backend, never echoed back). Same three keys the backend's SECRET_FILES
	// and the usage summary endpoint know about.
	const PROVIDER_ITEMS: { key: string; label: string; href: string }[] = [
		{ key: 'opencode', label: 'OpenCode', href: '#/opencode' },
		{ key: 'openrouter', label: 'OpenRouter', href: '#/openrouter' },
		{ key: 'nim', label: 'NVIDIA NIM', href: '#/nim' }
	];

	let fetchFailed = $state(false);
	let opencodePlan = $state<{ keys: { window: string; status: string; percent: number; resets_at: string }[] } | null>(
		null
	);
	let isExpanded = $state(false);
	let timer: ReturnType<typeof setInterval> | undefined;
	let refreshInFlight = false;

	async function refresh() {
		// setInterval fires on a fixed wall-clock cadence regardless of
		// whether the previous call resolved; without this guard, a slow
		// backend response (e.g. an MCP server taking a while to answer)
		// let ticks pile up as overlapping requests instead of just running
		// a little late.
		if (refreshInFlight) return;
		refreshInFlight = true;
		try {
			const res = await fetch(DASHBOARD_URL, { signal: AbortSignal.timeout(6000) });
			data = await res.json();
			fetchFailed = false;
		} catch {
			fetchFailed = true;
		} finally {
			refreshInFlight = false;
		}
	}

	function shortModelName(id: string): string {
		return id.split('/').pop() ?? id;
	}

	async function refreshSdServer() {
		try {
			const res = await fetch(SD_SERVER_URL, { signal: AbortSignal.timeout(6000) });
			sdServer = await res.json();
		} catch {
			// Same reasoning as the dashboard's refresh(): the
			// fetchFailed badge already reports a backend that is down.
			sdServer = null;
		}
	}

	/**
	 * Flip the image server on or off.
	 *
	 * The desired state, not the current one, drives the call: the switch
	 * hands over the value it wants, so a transition that is already under way
	 * cannot make the next click toggle the wrong way.
	 */
	async function setSdServer(on: boolean) {
		if (sdBusy) return;

		sdBusy = true;
		const action = on ? 'start' : 'stop';
		try {
			const res = await fetch(SD_CONTROL_URL, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ action }),
				signal: AbortSignal.timeout(95_000)
			});
			const body = await res.json().catch(() => null);
			if (body) sdServer = { ...body, server: sdServer?.server };

			if (!res.ok || !body?.ok) {
				toast.error(`Could not ${action} sd-server`, {
					description: body?.error ?? res.statusText
				});
				await refreshSdServer();

				return;
			}

			// systemctl returns as soon as the job is *queued*, so on start the
			// unit is usually still activating while the weights load. Wait for
			// the state to actually settle before clearing the spinner.
			const deadline = Date.now() + SD_SETTLE_TIMEOUT_MS;
			while (Date.now() < deadline) {
				await refreshSdServer();
				if (sdServer?.active === on && (on ? sdServer.server?.reachable : true)) break;
				await new Promise((r) => setTimeout(r, SD_SETTLE_POLL_MS));
			}

			if (on && sdServer?.active && !sdServer.server?.reachable) {
				toast.warning('sd-server is running but not answering yet', {
					description:
					'The unit is active, so the weights are still loading — image generation will start working on its own.'
				});
			} else {
				toast.success(on ? 'Image server started' : 'Image server stopped', {
					description: on
						? sdServer?.server?.model
							? shortModelName(sdServer.server.model)
							: undefined
						: 'Freed its VRAM and the model it was holding.'
				});
			}
		} catch (err) {
			toast.error(`Could not ${action} sd-server`, {
				description: err instanceof Error ? err.message : String(err)
			});
			await refreshSdServer();
		} finally {
			sdBusy = false;
		}
	}

	/**
	 * What the switch's right-hand side says. Deliberately shows the three
	 * states apart: an active unit whose HTTP endpoint has not answered yet is
	 * "starting…", not "on" — the difference matters because image tools will
	 * still fail for the next few seconds.
	 */
	const sdServerLabel = $derived.by(() => {
		const state = sdServer;
		if (!state) return sdServer === null ? 'unavailable' : '…';
		if (!state.available) return 'not installed';
		if (!state.active) return 'off';
		if (!state.server?.reachable) return 'starting…';
		return state.server.model ? shortModelName(state.server.model) : 'on';
	});

	/** Whether the switch is currently on — a unit that is up but not yet
	 * answering still counts as on, so the switch does not flicker back off
	 * while the model loads. */
	const sdServerOn = $derived(Boolean(sdServer?.active));

	async function refreshUsage() {
		// Separate from refresh()'s failure handling: the panel backend could,
		// in principle, be up while usage collection is not (or the file not
		// yet written) — a missing value just shows as '—' rather than
		// "unreachable".
		try {
			const res = await fetch(USAGE_SUMMARY_URL, { signal: AbortSignal.timeout(6000) });
			usage = await res.json();
		} catch {
			usage = null;
		}
	}

	function usageValue(key: string): string {
		const p = usage?.providers[key];
		const acct = usage?.openrouter_account;

		// OpenCode is the one provider with a REAL allowance figure: its Go plan
		// answers GET /usage with the three windows. The monthly is the binding
		// one (5h and weekly reset far sooner), so that is what the row shows —
		// a request count said nothing about how much plan was actually left.
		if (key === 'opencode' && opencodePlan) {
			const monthly = opencodePlan.keys.find((k) => k.window === 'monthly');
			if (monthly) return `${monthly.percent.toFixed(0)}% · month`;
		}

		if (!p) return '—';
		if (p.kind === 'spend') {
			// OpenRouter is the one provider with a real denominator: its own
			// account-wide usage out of total credits. The locally accumulated
			// per-model spend stays in the tooltip.
			if (acct && acct.total_credits > 0) {
				return `$${acct.total_usage.toFixed(2)} / $${acct.total_credits.toFixed(0)}`;
			}
			if (p.cost === 0) return '$0';

			return p.cost < 0.01 ? `$${p.cost.toFixed(4)}` : `$${p.cost.toFixed(2)}`;
		}
		// OpenCode now shows its real Go plan figure (see usageValue); NIM has no
		// published denominator at all, so its row still says how many requests,
		// not a proportion.
		return `${p.count_5h} req · 5h`;
	}

	function usageTooltip(key: string): string {
		const p = usage?.providers[key];

		// OpenCode Go plan windows, when the live read succeeded. Each window
		// carries its own reset instant, so the tooltip says when usage frees up
		// rather than leaving "66% used" looking permanent.
		if (key === 'opencode' && opencodePlan) {
			const LABELS: Record<string, string> = {
				rolling: 'Rolling 5h',
				weekly: 'Weekly',
				monthly: 'Monthly'
			};
			const lines = opencodePlan.keys.map((k) => {
				const reset = k.resets_at
					? ` · resets ${new Date(k.resets_at).toLocaleString()}`
					: '';
				return `${LABELS[k.window] ?? k.window}: ${k.percent.toFixed(0)}% used (${k.status})${reset}`;
			});
			if (p) {
				lines.push(
					`\nThis box: ${p.count_5h} requests in the last 5h · ${p.count_total} counted total`
				);
			}
			return lines.join('\n');
		}

		if (!p) return '';
		if (p.kind === 'spend') {
			const acct = usage?.openrouter_account;
			return [
				`${p.requests} requests · ${p.tokens} tokens\nsummed from every response's own usage block`,
				acct
					? `OpenRouter account: $${acct.total_usage.toFixed(4)} of\n$${acct.total_credits.toFixed(2)} (account-wide, from openrouter.ai)`
					: '',
				'Per-model detail: /openrouter-usage on the panel backend'
			]
				.filter(Boolean)
				.join('\n');
		}
		return [
			`${p.count_5h} requests in the last 5h · ${p.count_24h} in 24h`,
			`${p.count_total} since counting started (${p.total_window} retention)`,
			p.limit_known
				? 'Each model caps separately — its own fraction shows in the chat badge'
				: 'NVIDIA publishes no usage figure through its API, so no cap exists'
		]
			.filter(Boolean)
			.join('\n');
	}

	/**
	 * Fetch the in-flight task list.
	 *
	 * Swallows its own failure for the same reason refreshSdServer does: this
	 * is an auxiliary readout, and the dashboard's own fetchFailed badge
	 * already reports a backend that is down. Setting tasks=null hides the
	 * section rather than showing an error inside it.
	 */
	async function refreshTasks() {
		try {
			const res = await fetch(BACKGROUND_TASKS_URL, { signal: AbortSignal.timeout(6000) });
			tasks = await res.json();
		} catch {
			tasks = null;
		}
	}

	/**
	 * Fetch the live OpenCode Go plan allowance.
	 *
	 * Swallows its own failure like the other auxiliary readouts: a null here
	 * leaves the OpenCode row on its request-count fallback rather than
	 * showing an error inside a nav button.
	 */
	async function refreshOpencodePlan() {
		try {
			const res = await fetch(OPENCODE_PLAN_URL, { signal: AbortSignal.timeout(8000) });
			const body = await res.json();
			opencodePlan = Array.isArray(body?.keys) ? body : null;
		} catch {
			opencodePlan = null;
		}
	}

	onMount(() => {
		void refresh();
		void refreshUsage();
		void refreshSdServer();
		void refreshTasks();
		void refreshOpencodePlan();
		timer = setInterval(() => {
			void refresh();
			void refreshUsage();
			void refreshTasks();
			void refreshOpencodePlan();
			// Not while a start/stop is settling: setSdServer polls this same
			// endpoint every 2s during a transition, and a second reader here
			// would just race it with a staler answer.
			if (!sdBusy) void refreshSdServer();
		}, POLL_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

		<!-- Still a SvelteKit component rendered from +layout.svelte (see
	   the module docstring for the icon-as-prop workaround). -->
<!-- Mounted regardless of the panel's expanded state: it drives per-chat
     permission modes, which must work while the panel is closed. -->
<ChatPermissionSync />

<!-- Usage dashboard: only draws itself on the New Chat screen. -->
<DashboardOnNewChat />

<aside
	class={[
		'fixed md:sticky top-2 right-2 md:right-0 md:mr-2 md:mt-2 z-10 shrink-0',
		// No `md:w-auto` in this array: with it present, the generated stylesheet
		// put it after `md:w-72`, so the expanded panel sized itself to its content
		// (~620px with a long model name) instead of matching the left sidebar's
		// 288px. Collapsed (md:w-12) and expanded (md:w-72) now mirror the left
		// aside exactly; mobile keeps the full-width treatment.
		'w-[calc(100dvw-1rem)]',
		'md:h-[calc(100dvh-1.125rem)]',
		isExpanded && 'h-[calc(100dvh-1rem)]',
		'rounded-3xl md:rounded-2xl',
		'flex flex-col',
		'md:transition-[width,padding] duration-200 ease-out',
		isExpanded
			? 'bg-background md:bg-muted/60 md:backdrop-blur-xl border-border shadow-md md:w-72'
			: 'md:w-12',
		isExpanded && 'is-expanded'
	]}
>
	<div
		class="flex items-center justify-center {isExpanded ? 'px-2 pt-2' : 'px-0 pt-[1.125rem]'}"
	>
		<!-- No wordmark on the left of this row. An earlier revision mirrored the
		     left aside there (mark + "llama.cpp integrations"), which put a
		     second identity mark in a panel whose job is status/navigation, and
		     left the collapse button competing with it for the same row. This
		     row now holds exactly one control.

		     The mark survives in the COLLAPSED strip only, where it replaces the
		     bare expand chevron — that is the one place the panel is otherwise
		     unlabelled and a brand mark earns its space. -->
		<div
			class="flex items-center justify-center"
			onmouseenter={() => (logoHovered = true)}
			onmouseleave={() => (logoHovered = false)}
		>
			<!-- iconSize/class are split by state because the two states have
			     different budgets: expanded has the whole top row, collapsed has
			     a 3rem strip (md:w-12). The mark is transparent-backed, so its
			     strokes sit inside the box rather than filling it — hence a size
			     one step larger than a bare glyph would need. -->
			<ActionIcon
				icon={!isExpanded && logoHovered ? iconOpen : IntegrationsMark}
				size="lg"
				iconSize="h-4 w-4"
				class={isExpanded
					? 'h-9 w-9 rounded-full'
					: 'h-4 w-4 rounded pointer-events-auto'}
				onclick={() => (isExpanded = !isExpanded)}
				tooltip={isExpanded ? 'Collapse panel' : 'Open panel'}
				tooltipSide={TooltipSide.LEFT}
				ariaLabel={isExpanded ? 'Collapse panel' : 'Expand panel'}
			/>
		</div>
	</div>

	{#if isExpanded}
		<ScrollArea class="h-full">
			<div class="flex flex-col gap-1 p-2 pt-1">
				{#if fetchFailed}
					<Badge variant="destructive" class="mx-2 text-[10px]">panel backend unreachable</Badge>
				{/if}

				<!-- Incognito lives above Projects: it is a way to start a chat, and
				     the badge is the reminder that the open chat is unsaved. -->
				<div class="flex items-center gap-1 px-1">
					<Button
						variant="ghost"
						size="sm"
						class="h-7 flex-1 justify-start gap-2 px-2 text-[11px]"
						aria-label="Start an incognito chat"
						onclick={async () => {
							await startIncognitoChat();
							toast.message('Incognito chat', { description: INCOGNITO_SUMMARY });
						}}
					>
						<EyeOff class="h-3.5 w-3.5" />
						Incognito chat
					</Button>

					{#if incognitoChatStore.activeCount > 0}
						<Badge variant="secondary" class="shrink-0 text-[10px]">
							{incognitoChatStore.activeCount} unsaved
						</Badge>
					{/if}
				</div>

				<!-- Projects first: it is the thing you pick before starting a chat,
				     where Status and Providers are things you check afterwards. -->
				<ProjectsPanel />

				<!-- Snippets over a {#each} or inline duplication: these three
				     groups repeat the same button anatomy with different data,
				     and the section labels would drift apart the first time
				     somebody adds a group and copy-pastes only some of the
				     classes. Declared once here, rendered everywhere below. -->
				{#snippet sectionLabel(title: string)}
					<h3
						class="text-muted-foreground inline-flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium"
					>{title}</h3
					>
				{/snippet}

				<!-- Same anatomy as the left aside's nav buttons
				     (SidebarNavigationActions): Button variant=ghost at its size
				     default => h-9 text-sm font-medium, full width, trailing element
				     on the right. There the trailing slot is a keyboard hint; here
				     it is the usage figure, which is the information this panel has
				     that the left one does not. '—' hides the whole value slot, not
				     renders a dash — a row whose provider reports nothing reads as
				     a plain nav button, same as before usage was tracked. -->
				{#snippet sectionButton(label: string, href: string, value: string, tooltip: string)}
					<Button
						class="w-full justify-between px-2"
						variant="ghost"
						title={tooltip}
						onclick={() => goto(href)}
					>
						<span class="min-w-0 truncate">{label}</span>
						{#if value !== '—'}
							<span
								class="shrink-0 pl-2 text-right text-[11px] tabular-nums text-muted-foreground"
							>{value}</span
							>
						{/if}
					</Button>
				{/snippet}

				{#if data}
					{@render sectionLabel('Status')}

					<div
						class="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-foreground/5"
					>
						<span class="text-xs text-foreground">llama-server</span>
						{#if !data.llama_server.reachable}
							<Badge variant="destructive" class="text-[10px]">down</Badge>
						{:else if data.llama_server.loaded?.length}
							<span class="truncate pl-2 text-right text-[11px] text-muted-foreground"
								>{shortModelName(data.llama_server.loaded[0])}</span
							>
						{:else}
							<Badge variant="secondary" class="text-[10px]">idle</Badge>
						{/if}
					</div>
					<!-- sd-server: the one control in this panel that changes the
					     machine rather than reporting on it. The switch drives the
					     systemd user unit through POST /api/sd-server/control; the
					     label beside it distinguishes off / starting / on, because a
					     unit that is up but still loading weights is not yet usable. -->
					<div
						class="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-foreground/5"
					>
						<div class="flex min-w-0 flex-col">
							<span class="text-xs text-foreground">sd-server</span>
							{#if sdBusy}
								<span class="text-[10px] text-muted-foreground">working…</span>
							{:else}
								<span class="truncate text-[10px] text-muted-foreground">{sdServerLabel}</span>
							{/if}
						</div>
						<Switch
							checked={sdServerOn}
							disabled={sdBusy || !sdServer?.available}
							aria-label="Start or stop the image server"
							onCheckedChange={(checked) => setSdServer(Boolean(checked))}
						/>
					</div>

				{:else if !fetchFailed}
					<p class="px-2 text-xs text-muted-foreground">Loading…</p>
				{/if}

				<div class="my-1 border-t border-border"></div>

				<!-- Background tasks: what is happening but has not finished.
				     Placed after Status because it explains it — a relay row here
				     is the reason a provider's counter is climbing, and the
				     looping flag is the reason it climbs without ever answering. -->
				{#if tasks && tasks.tasks.length > 0}
					{@render sectionLabel('Background tasks')}

					{#each tasks.tasks as task (task.kind === 'relay'
						? `relay:${task.provider}:${task.model}`
						: `load:${task.model}`)}
						<div class="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-foreground/5">
							<div class="flex min-w-0 flex-col">
								{#if task.kind === 'relay'}
									<span class="truncate text-xs text-foreground"
										>{shortModelName(task.model)}</span
									>
									<span class="truncate text-[10px] text-muted-foreground">
										{task.provider} · {task.messages} messages
										{#if task.tools !== null}· {task.tools} tools{/if}
									</span>
								{:else}
									<span class="truncate text-xs text-foreground">{task.model ?? 'model'}</span>
									<span class="truncate text-[10px] text-muted-foreground">{task.state}</span>
								{/if}
							</div>
							{#if task.kind === 'relay'}
								<!-- rejected: red only when repair is not converging — two or
								     more refusals on the same model. One rejection is routine
								     (the relay repairs and carries on) and gets a neutral
								     count. Deliberately NOT keyed off a growing message
								     count: a healthy multi-turn chat grows identically. -->
								{#if task.looping}
									<Badge
										variant="destructive"
										class="shrink-0 text-[10px]"
										title={task.last_rejection ??
											`${task.rejections} rejections across ${task.turns} requests`}
										>{task.rejections} rejected</Badge
									>
								{:else if task.rejections > 0}
									<Badge variant="secondary" class="shrink-0 text-[10px]"
										title={task.last_rejection ?? 'one rejection, recovered'}
										>{task.turns} · 1 fixed</Badge
									>
								{:else}
									<Badge variant="outline" class="shrink-0 text-[10px]">
										{task.turns > 1 ? `${task.turns} turns` : 'running'}
									</Badge>
								{/if}
							{:else}
								<Badge variant="secondary" class="shrink-0 text-[10px]"
									>{task.progress != null
										? `${Math.round(task.progress * 100)}%`
										: 'loading'}</Badge
								>
							{/if}
						</div>
					{/each}
				{/if}

				<div class="my-1 border-t border-border"></div>

				<!-- Remote providers, each opening its own page, with whatever
			     usage its provider publishes reported on the right (the
			     chat-form badge is per model; this is the whole-provider roll).
			     The API key editor lives on each provider's own page, so this
			     list stays link-only — open a page there to set or replace
			     that provider's key. -->
				{@render sectionLabel('Providers')}

				{#each PROVIDER_ITEMS as item (item.key)}
					{@render sectionButton(item.label, item.href, usageValue(item.key), usageTooltip(item.key))}
				{/each}

				{@render sectionLabel('Data')}
				{@render sectionButton('RAG Editor', '#/rag', '—', 'Index, upload and manage documents for retrieval')}

			</div>
		</ScrollArea>
	{/if}
</aside>

<style>
	/* Mirrors SidebarNavigation.svelte's mobile drawer treatment: collapsed
	   opts back in via pointer-events-auto), expanded shows a full-bleed
	   strip lets taps through to the chat behind it (only the toggle button
	   blurred backdrop so the drawer reads as a modal rather than a stray box. */
	@media (max-width: 768px) {
		aside:not(.is-expanded) {
			pointer-events: none;
		}

		aside.is-expanded::before {
			content: '';
			position: fixed;
			inset: -0.5rem;
			z-index: -1;
			background: var(--background);
			backdrop-filter: blur(1rem);
			pointer-events: none;
		}
	}
</style>
