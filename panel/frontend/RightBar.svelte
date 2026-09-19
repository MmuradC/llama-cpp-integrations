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
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { ScrollArea } from '$lib/components/ui/scroll-area';
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

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const DASHBOARD_URL = `${PANEL_ORIGIN}/api/dashboard`;
	const SECRETS_URL = `${PANEL_ORIGIN}/api/secrets`;
	const USAGE_SUMMARY_URL = `${PANEL_ORIGIN}/api/usage-summary`;
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

	let data = $state<Dashboard | null>(null);
	let usage = $state<UsageSummary | null>(null);

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

	let secretsSet = $state<Record<string, boolean>>({});
	let secretDrafts = $state<Record<string, string>>({});
	let secretBusy = $state<Record<string, boolean>>({});
	let secretMessage = $state<Record<string, string>>({});

	let fetchFailed = $state(false);
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

	async function refreshSecretsStatus() {
		try {
			const res = await fetch(SECRETS_URL, { signal: AbortSignal.timeout(6000) });
			secretsSet = await res.json();
		} catch {
			// dashboard's own fetchFailed badge already reports backend-down; a
			// second badge here would be redundant, so this fails silently
		}
	}

	async function saveSecret(key: string) {
		const value = (secretDrafts[key] ?? '').trim();
		if (!value) return;

		secretBusy = { ...secretBusy, [key]: true };
		secretMessage = { ...secretMessage, [key]: '' };

		try {
			const res = await fetch(`${SECRETS_URL}/${key}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ value }),
				signal: AbortSignal.timeout(6000)
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);

			secretDrafts = { ...secretDrafts, [key]: '' };
			secretMessage = { ...secretMessage, [key]: 'Saved' };
			await refreshSecretsStatus();
		} catch (err) {
			secretMessage = { ...secretMessage, [key]: err instanceof Error ? err.message : 'Failed' };
		} finally {
			secretBusy = { ...secretBusy, [key]: false };
		}
	}

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
		// OpenCode cannot show one aggregate fraction (each of its models caps
		// separately) and NIM has no published denominator at all — so the row
		// says how many requests, not a proportion.
		return `${p.count_5h} req · 5h`;
	}

	function usageTooltip(key: string): string {
		const p = usage?.providers[key];
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

	onMount(() => {
		void refresh();
		void refreshUsage();
		void refreshSecretsStatus();
		timer = setInterval(() => {
			void refresh();
			void refreshUsage();
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
	<div class="flex items-center {isExpanded ? 'justify-end' : 'justify-center'} px-2 pt-2">
		<ActionIcon
			icon={isExpanded ? iconClose : iconOpen}
			size="lg"
			iconSize="h-4 w-4"
			class="h-9 w-9 rounded-full hover:bg-foreground/10! pointer-events-auto"
			onclick={() => (isExpanded = !isExpanded)}
			tooltip={isExpanded ? 'Collapse panel' : 'Open panel'}
			tooltipSide={TooltipSide.LEFT}
			ariaLabel={isExpanded ? 'Collapse panel' : 'Expand panel'}
		/>
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
					<div
						class="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-foreground/5"
					>
						<span class="text-xs text-foreground">sd-server</span>
						{#if !data.sd_server.reachable}
							<Badge variant="secondary" class="text-[10px]">stopped</Badge>
						{:else}
							<span class="truncate pl-2 text-right text-[11px] text-muted-foreground"
								>{data.sd_server.model ? shortModelName(data.sd_server.model) : 'ready'}</span
							>
						{/if}
					</div>

				{:else if !fetchFailed}
					<p class="px-2 text-xs text-muted-foreground">Loading…</p>
				{/if}

				<div class="my-1 border-t border-border"></div>

				<!-- Remote providers, each opening its own page, with whatever
				     usage its provider publishes reported on the right (the
				     chat-form badge is per model; this is the whole-provider roll). -->
				<!-- Remote providers, each opening its own page, with whatever
			     usage its provider publishes reported on the right (the
			     chat-form badge is per model; this is the whole-provider roll).
			     The API key sits directly under its own provider: a missing key
			     is the reason that row's requests fail, so the two belong
			     together rather than a secrets drawer further down. -->
				{@render sectionLabel('Providers')}
				{#snippet providerSecret(key: string)}
					<!-- Secret row, indented under its provider row. Submit-once:
					     the input clears on save and the status badge tells whether
					     a key is on disk — the value itself is never read back. -->
					<div class="flex items-center gap-1.5 pl-7 pr-1 pb-1.5">
						<span class="shrink-0 text-[10px] text-muted-foreground">API key</span>
						{#if secretsSet[key]}
							<Badge variant="secondary" class="shrink-0 px-1.5 text-[10px]">configured</Badge>
						{:else}
							<Badge variant="outline" class="shrink-0 px-1.5 text-[10px]">not set</Badge>
						{/if}
						<Input
							type="password"
							autocomplete="new-password"
							placeholder="sk-..."
							class="h-6 min-w-0 flex-1 text-xs"
							bind:value={secretDrafts[key]}
							onkeydown={(e) => e.key === 'Enter' && saveSecret(key)}
						/>
						<Button
							size="sm"
							variant="secondary"
							class="h-6 shrink-0 px-2 text-[10px]"
							disabled={!secretDrafts[key]?.trim() || secretBusy[key]}
							onclick={() => saveSecret(key)}
						>
							{secretBusy[key] ? '…' : 'Save'}
						</Button>
					</div>
					{#if secretMessage[key]}
						<span
							class="pl-7 text-[10px] {secretMessage[key] === 'Saved'
								? 'text-emerald-500'
								: 'text-destructive'}">{secretMessage[key]}</span
						>
					{/if}
				{/snippet}

				{#each PROVIDER_ITEMS as item (item.key)}
					{@render sectionButton(item.label, item.href, usageValue(item.key), usageTooltip(item.key))}
					{@render providerSecret(item.key)}
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
