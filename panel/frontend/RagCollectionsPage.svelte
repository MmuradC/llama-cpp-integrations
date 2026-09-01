<script lang="ts">
	// Lives entirely outside llama.cpp's own source tree — see ../README.md.
	// Reached from the "RAG Collections" button in the left sidebar (see
	// SIDEBAR_ACTIONS_ITEMS in ui.constants.ts). The route file that makes
	// this discoverable to SvelteKit's router is
	// tools/ui/src/routes/rag-collections/+page.svelte.
	//
	// Visually cloned from SettingsMcpServers.svelte (title bar, Empty state,
	// responsive card grid) but the data source is fundamentally different:
	// MCP servers are client-side settingsStore/localStorage state, RAG
	// collections are server-owned (files on disk via the panel backend), so
	// this fetches directly from :9010 instead of reading a store — same
	// storeless fetch pattern OpenRouterPage.svelte already uses for its own
	// server-owned data.
	//
	// Upload-only is RagPage.svelte's job (right sidebar panel) — this page
	// is list/browse/delete only, per how this was actually asked for.
	import type { Component } from 'svelte';
	import * as AlertDialog from '$lib/components/ui/alert-dialog';
	import { Button } from '$lib/components/ui/button';
	import * as Empty from '$lib/components/ui/empty';
	import { onMount } from 'svelte';

	interface Props {
		iconExpand: Component;
		iconDelete: Component;
		iconClose: Component;
	}

	let { iconClose: IconClose, iconDelete: IconDelete, iconExpand: IconExpand }: Props = $props();

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const COLLECTIONS_URL = `${PANEL_ORIGIN}/api/rag/collections`;

	type RagDocument = {
		doc_id: string;
		filename: string;
		bytes: number;
		added_at: string;
		status: 'processing' | 'ready' | 'error';
		error: string | null;
		chunk_count: number;
	};

	type RagCollection = {
		id: string;
		name: string;
		description: string;
		created_at: string;
		chunk_count: number;
		documents: RagDocument[];
	};

	let collections = $state<RagCollection[] | null>(null);
	let loadError = $state('');
	let expanded = $state<Set<string>>(new Set());

	async function load() {
		try {
			const res = await fetch(COLLECTIONS_URL, { signal: AbortSignal.timeout(10000) });
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			collections = await res.json();
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load collections';
		}
	}

	onMount(() => void load());

	function toggleExpanded(id: string) {
		const next = new Set(expanded);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		expanded = next;
	}

	function formatBytes(n: number): string {
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}

	type PendingDelete =
		| { kind: 'collection'; id: string; name: string }
		| { kind: 'document'; collectionId: string; docId: string; name: string };

	// pendingDelete is never nulled directly - only replaced with a new target
	// or left as-is while deleteDialogOpen goes false, so the dialog's own
	// close transition still has valid data to render instead of briefly
	// flashing a "no target" default state as it fades out.
	let pendingDelete = $state<PendingDelete | null>(null);
	let deleteDialogOpen = $state(false);
	let deleteError = $state('');

	function requestDelete(target: PendingDelete) {
		pendingDelete = target;
		deleteDialogOpen = true;
		deleteError = '';
	}

	async function confirmDelete() {
		if (!pendingDelete) return;
		deleteError = '';
		try {
			const url =
				pendingDelete.kind === 'collection'
					? `${COLLECTIONS_URL}/${pendingDelete.id}`
					: `${COLLECTIONS_URL}/${pendingDelete.collectionId}/documents/${pendingDelete.docId}`;
			const res = await fetch(url, { method: 'DELETE', signal: AbortSignal.timeout(10000) });
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);
			deleteDialogOpen = false;
			await load();
		} catch (err) {
			deleteError = err instanceof Error ? err.message : 'Delete failed';
		}
	}
</script>

<div class="flex min-h-[calc(100dvh-4rem)] flex-col">
	<div class="fixed top-4.5 right-4 z-50 md:hidden">
		<button
			type="button"
			class="rounded-full p-2 hover:bg-foreground/10"
			onclick={() => history.back()}
			aria-label="Close"
		>
			<IconClose class="h-4 w-4" />
		</button>
	</div>

	<div
		class="sticky top-0 z-10 mt-4 mb-2 flex items-start gap-4 md:p-4 p-0 px-4 md:justify-between md:px-8"
	>
		<div class="flex items-center gap-2">
			<h1 class="text-lg font-semibold md:text-2xl">RAG Collections</h1>
		</div>
	</div>

	{#if loadError}
		<p class="px-4 text-sm text-destructive md:px-8">{loadError}</p>
	{/if}

	{#if collections && collections.length === 0}
		<div class="flex flex-1 items-center justify-center py-16">
			<Empty.Root class="max-w-md">
				<Empty.Header>
					<Empty.Title>No RAG collections yet</Empty.Title>
					<Empty.Description>
						Upload documents from the "Manage RAG documents" link in the right sidebar to create
						one.
					</Empty.Description>
				</Empty.Header>
			</Empty.Root>
		</div>
	{:else if collections}
		<div
			class="grid gap-3 px-4 md:px-8"
			style="grid-template-columns: repeat(auto-fill, minmax(min(32rem, calc(100dvw - 2rem)), 1fr));"
		>
			{#each collections as c (c.id)}
				{@const isOpen = expanded.has(c.id)}
				<div class="flex flex-col gap-3 rounded-lg border bg-muted/30 p-4">
					<div class="flex items-start justify-between gap-2">
						<div class="flex min-w-0 flex-col">
							<span class="truncate text-sm font-medium text-foreground">{c.name}</span>
							{#if c.description}
								<span class="truncate text-xs text-muted-foreground">{c.description}</span>
							{/if}
							<span class="text-xs text-muted-foreground">
								{c.documents.length} document(s) · {c.chunk_count} chunks · created {new Date(
									c.created_at
								).toLocaleDateString()}
							</span>
						</div>
						<div class="flex shrink-0 items-center gap-1">
							<button
								type="button"
								class="rounded-md p-1.5 text-muted-foreground hover:bg-foreground/10 hover:text-foreground {isOpen
									? 'rotate-180'
									: ''}"
								onclick={() => toggleExpanded(c.id)}
								aria-label={isOpen ? 'Collapse' : 'Expand'}
							>
								<IconExpand class="h-4 w-4" />
							</button>
							<button
								type="button"
								class="rounded-md p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
								onclick={() => requestDelete({ kind: 'collection', id: c.id, name: c.name })}
								aria-label="Delete collection"
							>
								<IconDelete class="h-4 w-4" />
							</button>
						</div>
					</div>

					{#if isOpen}
						<div class="flex flex-col gap-1 border-t border-border pt-2">
							{#if c.documents.length === 0}
								<span class="text-xs text-muted-foreground">No documents yet.</span>
							{/if}
							{#each c.documents as doc (doc.doc_id)}
								<div class="flex items-center justify-between gap-2 rounded-md px-2 py-1 text-xs hover:bg-foreground/5">
									<div class="flex min-w-0 flex-col">
										<span class="truncate text-foreground">{doc.filename}</span>
										<span class="text-muted-foreground">
											{formatBytes(doc.bytes)} ·
											{#if doc.status === 'ready'}
												{doc.chunk_count} chunks
											{:else if doc.status === 'processing'}
												processing…
											{:else}
												error: {doc.error}
											{/if}
										</span>
									</div>
									<button
										type="button"
										class="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
										onclick={() =>
											requestDelete({
												kind: 'document',
												collectionId: c.id,
												docId: doc.doc_id,
												name: doc.filename
											})}
										aria-label="Delete document"
									>
										<IconDelete class="h-3.5 w-3.5" />
									</button>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<AlertDialog.Root bind:open={deleteDialogOpen}>
	<AlertDialog.Content>
		<AlertDialog.Header>
			<AlertDialog.Title>
				{pendingDelete?.kind === 'collection' ? 'Delete Collection' : 'Delete Document'}
			</AlertDialog.Title>
			<AlertDialog.Description>
				Are you sure you want to delete <strong>{pendingDelete?.name}</strong>? This action cannot
				be undone.
				{#if deleteError}
					<span class="mt-2 block text-destructive">{deleteError}</span>
				{/if}
			</AlertDialog.Description>
		</AlertDialog.Header>
		<AlertDialog.Footer>
			<AlertDialog.Cancel>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="bg-destructive text-destructive-foreground hover:bg-destructive/90"
				onclick={confirmDelete}
			>
				Delete
			</AlertDialog.Action>
		</AlertDialog.Footer>
	</AlertDialog.Content>
</AlertDialog.Root>
