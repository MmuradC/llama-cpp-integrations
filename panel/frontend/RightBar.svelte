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
	import { onDestroy, onMount } from 'svelte';

	interface Props {
		iconOpen: Component;
		iconClose: Component;
	}

	let { iconOpen, iconClose }: Props = $props();

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const DASHBOARD_URL = `${PANEL_ORIGIN}/api/dashboard`;
	const SECRETS_URL = `${PANEL_ORIGIN}/api/secrets`;
	const POLL_MS = 4000;

	// Writing a key here goes straight to disk from panel/backend/server.py —
	// it never passes through the local model's own context the way asking it
	// in chat to write the file would. The value is submit-once: cleared from
	// the input immediately after a successful save, and never re-fetched
	// (the status endpoint returns only whether each key is set, not its value).
	const SECRET_FIELDS: { key: string; label: string }[] = [
		{ key: 'openrouter', label: 'OpenRouter' },
		{ key: 'nim', label: 'NVIDIA NIM' }
	];

	let secretsSet = $state<Record<string, boolean>>({});
	let secretDrafts = $state<Record<string, string>>({});
	let secretBusy = $state<Record<string, boolean>>({});
	let secretMessage = $state<Record<string, string>>({});

	type McpServerStatus = {
		name: string;
		ok: boolean;
		tool_count?: number;
		ms?: number;
		error?: string;
	};

	type Gpu = {
		index: number;
		name: string;
		total_mib: number;
		used_mib: number;
		free_mib: number;
		util_pct: number;
	};

	type Dashboard = {
		mcp_servers: McpServerStatus[];
		gpus: Gpu[];
		llama_server: { reachable: boolean; loaded?: string[] };
		sd_server: { reachable: boolean; model?: string | null };
	};

	let data = $state<Dashboard | null>(null);
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

	onMount(() => {
		void refresh();
		void refreshSecretsStatus();
		timer = setInterval(refresh, POLL_MS);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<aside
	class={[
		'top-2 mt-2 mr-2 mb-2 md:sticky md:h-[calc(100dvh-1.125rem)] shrink-0',
		'rounded-3xl md:rounded-2xl',
		'flex flex-col',
		'md:transition-[width,padding] duration-200 ease-out',
		isExpanded ? 'md:w-72 md:bg-muted/60 md:backdrop-blur-xl border-border shadow-md' : 'md:w-12'
	]}
>
	<div class="flex items-center {isExpanded ? 'justify-between' : 'justify-center'} px-2 pt-2">
		{#if isExpanded}
			<h2 class="text-sm font-medium text-foreground">MCP dashboard</h2>
		{/if}
		<ActionIcon
			icon={isExpanded ? iconClose : iconOpen}
			size="lg"
			iconSize="h-4 w-4"
			class="h-9 w-9 rounded-full hover:bg-foreground/10!"
			onclick={() => (isExpanded = !isExpanded)}
			tooltip={isExpanded ? 'Close MCP dashboard' : 'Open MCP dashboard'}
			tooltipSide={TooltipSide.LEFT}
			ariaLabel={isExpanded ? 'Collapse MCP dashboard' : 'Expand MCP dashboard'}
		/>
	</div>

	{#if isExpanded}
		<ScrollArea class="h-full">
			<div class="flex flex-col gap-1 p-2 pt-1">
				{#if fetchFailed}
					<Badge variant="destructive" class="mx-2 text-[10px]">panel backend unreachable</Badge>
				{/if}

				{#if data}
					<!-- GPU -->
					{#each data.gpus as gpu (gpu.index)}
						{@const usedPct = Math.round((gpu.used_mib / gpu.total_mib) * 100)}
						<div class="flex flex-col gap-1.5 rounded-md px-2 py-1.5">
							<span class="text-xs text-foreground">{gpu.name}</span>
							<div class="h-1.5 w-full overflow-hidden rounded-full bg-foreground/10">
								<div
									class="h-full rounded-full bg-primary transition-all"
									style="width: {usedPct}%"
								></div>
							</div>
							<div class="flex justify-between text-[11px] text-muted-foreground">
								<span
									>{(gpu.used_mib / 1024).toFixed(1)} / {(gpu.total_mib / 1024).toFixed(1)} GiB</span
								>
								<span>{gpu.util_pct}% util</span>
							</div>
						</div>
					{/each}

					<!-- llama-server / sd-server -->
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

					<div class="my-1 border-t border-border"></div>

					<!-- 9 MCP servers -->
					{#each data.mcp_servers as server (server.name)}
						<div
							class="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-foreground/5"
						>
							<div class="flex min-w-0 items-center gap-2">
								<span
									class="h-1.5 w-1.5 shrink-0 rounded-full {server.ok
										? 'bg-emerald-500'
										: 'bg-destructive'}"
								></span>
								<span class="truncate text-xs text-foreground">{server.name}</span>
							</div>
							{#if server.ok}
								<Badge variant="outline" class="shrink-0 text-[10px] tabular-nums"
									>{server.tool_count}</Badge
								>
							{:else}
								<Badge variant="destructive" class="shrink-0 text-[10px]">err</Badge>
							{/if}
						</div>
					{/each}
				{:else if !fetchFailed}
					<p class="px-2 text-xs text-muted-foreground">Loading…</p>
				{/if}

				<div class="my-1 border-t border-border"></div>

				<!-- Secrets: writes straight to disk via the panel backend, not
				     through chat and not through the MCP servers' own Authorization
				     field (that header never reaches a stdio server — see
				     panel/README.md). Submit-once: never shows a saved value back. -->
				<div class="flex flex-col gap-2 px-2 py-1.5">
					<span class="text-xs text-foreground">Secrets</span>
					{#each SECRET_FIELDS as field (field.key)}
						<div class="flex flex-col gap-1">
							<div class="flex items-center justify-between">
								<span class="text-[11px] text-muted-foreground">{field.label}</span>
								{#if secretsSet[field.key]}
									<Badge variant="secondary" class="text-[10px]">configured</Badge>
								{:else}
									<Badge variant="outline" class="text-[10px]">not set</Badge>
								{/if}
							</div>
							<div class="flex gap-1.5">
								<Input
									type="password"
									autocomplete="new-password"
									placeholder="sk-..."
									class="h-7 text-xs"
									bind:value={secretDrafts[field.key]}
									onkeydown={(e) => e.key === 'Enter' && saveSecret(field.key)}
								/>
								<Button
									size="sm"
									variant="secondary"
									class="h-7 shrink-0 px-2 text-[11px]"
									disabled={!secretDrafts[field.key]?.trim() || secretBusy[field.key]}
									onclick={() => saveSecret(field.key)}
								>
									{secretBusy[field.key] ? '…' : 'Save'}
								</Button>
							</div>
							{#if secretMessage[field.key]}
								<span
									class="text-[10px] {secretMessage[field.key] === 'Saved'
										? 'text-emerald-500'
										: 'text-destructive'}">{secretMessage[field.key]}</span
								>
							{/if}
						</div>
					{/each}
				</div>

				<Button
					size="sm"
					variant="ghost"
					class="h-7 justify-start px-2 text-[11px] text-muted-foreground hover:text-foreground"
					onclick={() => goto('#/openrouter')}
				>
					Browse OpenRouter models →
				</Button>
				<Button
					size="sm"
					variant="ghost"
					class="h-7 justify-start px-2 text-[11px] text-muted-foreground hover:text-foreground"
					onclick={() => goto('#/nim')}
				>
					Browse NVIDIA NIM models →
				</Button>
				<Button
					size="sm"
					variant="ghost"
					class="h-7 justify-start px-2 text-[11px] text-muted-foreground hover:text-foreground"
					onclick={() => goto('#/rag')}
				>
					Manage RAG documents →
				</Button>
			</div>
		</ScrollArea>
	{/if}
</aside>
