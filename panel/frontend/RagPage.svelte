<script lang="ts">
	// Lives entirely outside llama.cpp's own source tree — see ../README.md.
	// Reached from the "Manage RAG documents" link in RightBar.svelte. The
	// route file that makes this discoverable to SvelteKit's router is
	// tools/ui/src/routes/rag/+page.svelte, same stub pattern as OpenRouterPage.
	//
	// Deliberately upload-only: creating/picking a collection, dropping or
	// picking files, watching them ingest. Browsing what is already in a
	// collection is RagCollectionsPage.svelte's job (reached from the left
	// sidebar) — kept as two separate pages rather than one, per how this was
	// actually asked for.
	//
	// No @lucide/svelte import here — this file lives outside tools/ui, where
	// a bare package specifier resolves node_modules relative to ITS OWN path
	// (see RightBar.svelte's comment on the same constraint). OpenRouterPage
	// sidesteps this with plain glyphs; this page does the same.
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import { onDestroy, onMount } from 'svelte';

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const COLLECTIONS_URL = `${PANEL_ORIGIN}/api/rag/collections`;
	const POLL_MS = 2000;

	type RagDocument = {
		doc_id: string;
		filename: string;
		bytes: number;
		status: 'processing' | 'ready' | 'error';
		error: string | null;
		chunk_count: number;
	};

	type RagCollection = {
		id: string;
		name: string;
		description: string;
		chunk_count: number;
		documents: RagDocument[];
	};

	let collections = $state<RagCollection[] | null>(null);
	let loadError = $state('');
	let selectedId = $state('');

	let isCreating = $state(false);
	let newName = $state('');
	let newDescription = $state('');
	let createError = $state('');

	async function loadCollections() {
		try {
			const res = await fetch(COLLECTIONS_URL, { signal: AbortSignal.timeout(10000) });
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			collections = await res.json();
			if (!selectedId && collections?.length) selectedId = collections[0].id;
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load collections';
		}
	}

	async function createCollection() {
		const name = newName.trim();
		if (!name) return;

		createError = '';
		try {
			const res = await fetch(COLLECTIONS_URL, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name, description: newDescription.trim() }),
				signal: AbortSignal.timeout(10000)
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);
			const created: RagCollection = await res.json();
			await loadCollections();
			selectedId = created.id;
			isCreating = false;
			newName = '';
			newDescription = '';
		} catch (err) {
			createError = err instanceof Error ? err.message : 'Failed to create collection';
		}
	}

	onMount(() => void loadCollections());

	// Upload/ingestion progress for the current batch — cleared per file once
	// it either finishes or fails, not tied to any global list of documents
	// (that view belongs to RagCollectionsPage, not here).
	type UploadRow = {
		filename: string;
		status: 'uploading' | 'processing' | 'ready' | 'error';
		error?: string;
		chunkCount?: number;
	};
	let uploadRows = $state<UploadRow[]>([]);
	let isBusy = $derived(uploadRows.some((r) => r.status === 'uploading' || r.status === 'processing'));

	async function pollUntilDone(collectionId: string, filename: string, rowIndex: number) {
		for (;;) {
			await new Promise((r) => setTimeout(r, POLL_MS));
			try {
				const res = await fetch(`${COLLECTIONS_URL}/${collectionId}`, {
					signal: AbortSignal.timeout(8000)
				});
				const collection: RagCollection = await res.json();
				const doc = [...collection.documents].reverse().find((d) => d.filename === filename);
				if (!doc) continue;

				if (doc.status === 'ready') {
					uploadRows[rowIndex] = { filename, status: 'ready', chunkCount: doc.chunk_count };
					return;
				}
				if (doc.status === 'error') {
					uploadRows[rowIndex] = { filename, status: 'error', error: doc.error ?? 'ingestion failed' };
					return;
				}
			} catch {
				// transient poll failure — keep trying, the collection GET is cheap
			}
		}
	}

	async function uploadOne(collectionId: string, file: File) {
		const rowIndex = uploadRows.length;
		uploadRows = [...uploadRows, { filename: file.name, status: 'uploading' }];

		const body = new FormData();
		body.set('file', file);

		try {
			const res = await fetch(`${COLLECTIONS_URL}/${collectionId}/documents`, {
				method: 'POST',
				body
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);
			uploadRows[rowIndex] = { filename: file.name, status: 'processing' };
			await pollUntilDone(collectionId, file.name, rowIndex);
			await loadCollections();
		} catch (err) {
			uploadRows[rowIndex] = {
				filename: file.name,
				status: 'error',
				error: err instanceof Error ? err.message : 'Upload failed'
			};
		}
	}

	async function uploadFiles(files: FileList | File[]) {
		if (!selectedId) return;
		// sequential, not parallel: the router's single embedding slot
		// serializes these anyway, and it keeps this progress list simple
		for (const file of Array.from(files)) {
			await uploadOne(selectedId, file);
		}
	}

	function handleFilePick(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		if (input.files?.length) void uploadFiles(input.files);
		input.value = '';
	}

	// Nesting-counter drag tracking, same idea as
	// use-chat-screen-drag-and-drop.svelte.ts in tools/ui (not imported —
	// that hook is chat/modality-coupled and lives inside tools/ui's own
	// $lib tree, unreachable from here).
	let dragDepth = $state(0);
	let isDragging = $derived(dragDepth > 0);

	function handleDragEnter(e: DragEvent) {
		e.preventDefault();
		dragDepth += 1;
	}
	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		dragDepth = Math.max(0, dragDepth - 1);
	}
	function handleDragOver(e: DragEvent) {
		e.preventDefault();
	}
	function handleDrop(e: DragEvent) {
		e.preventDefault();
		dragDepth = 0;
		if (e.dataTransfer?.files.length) void uploadFiles(e.dataTransfer.files);
	}

	onDestroy(() => {
		uploadRows = [];
	});
</script>

<div class="mx-auto flex max-w-4xl flex-col gap-4 p-6">
	<div>
		<h1 class="text-lg font-medium text-foreground">RAG documents</h1>
		<p class="text-sm text-muted-foreground">
			Upload documents into a collection so rag_query can ground answers in them. Browse or delete
			what's already there from the RAG Collections button in the left sidebar.
		</p>
	</div>

	{#if loadError}
		<Badge variant="destructive">{loadError}</Badge>
	{/if}

	<div class="flex flex-col gap-2">
		<span class="text-xs font-medium text-muted-foreground">Collection</span>
		<div class="flex items-center gap-2">
			<select
				class="h-9 flex-1 rounded-md border border-input bg-transparent px-3 text-sm"
				bind:value={selectedId}
				disabled={!collections?.length}
			>
				{#if !collections?.length}
					<option value="">No collections yet</option>
				{/if}
				{#each collections ?? [] as c (c.id)}
					<option value={c.id}>{c.name} ({c.chunk_count} chunks)</option>
				{/each}
			</select>
			<Button size="sm" variant="secondary" onclick={() => (isCreating = !isCreating)}>
				+ New collection
			</Button>
		</div>

		{#if isCreating}
			<div class="flex flex-col gap-2 rounded-md border border-border p-3">
				<Input placeholder="Collection name" bind:value={newName} />
				<Input placeholder="Description (optional)" bind:value={newDescription} />
				{#if createError}
					<Badge variant="destructive">{createError}</Badge>
				{/if}
				<div class="flex justify-end gap-2">
					<Button size="sm" variant="ghost" onclick={() => (isCreating = false)}>Cancel</Button>
					<Button size="sm" disabled={!newName.trim()} onclick={createCollection}>Create</Button>
				</div>
			</div>
		{/if}
	</div>

	<div
		class="flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-10 text-center {isDragging
			? 'border-primary bg-primary/5'
			: 'border-border'} {!selectedId ? 'pointer-events-none opacity-50' : ''}"
		ondragenter={handleDragEnter}
		ondragleave={handleDragLeave}
		ondragover={handleDragOver}
		ondrop={handleDrop}
	>
		<p class="text-sm text-muted-foreground">Drag and drop files here, or</p>
		<label class="cursor-pointer">
			<span class="inline-flex h-9 items-center rounded-md bg-secondary px-4 text-sm text-secondary-foreground hover:bg-secondary/80">
				Choose files
			</span>
			<input
				type="file"
				multiple
				class="hidden"
				disabled={!selectedId}
				onchange={handleFilePick}
			/>
		</label>
		<p class="text-xs text-muted-foreground">.txt, .md, .pdf, .docx, .pptx, .xlsx, .html</p>
	</div>

	{#if uploadRows.length > 0}
		<div class="flex flex-col gap-1.5">
			{#each uploadRows as row (row.filename + row.status)}
				<div class="flex items-center justify-between rounded-md border border-border px-3 py-2 text-sm">
					<span class="truncate text-foreground">{row.filename}</span>
					{#if row.status === 'uploading'}
						<Badge variant="secondary">uploading…</Badge>
					{:else if row.status === 'processing'}
						<Badge variant="secondary">processing…</Badge>
					{:else if row.status === 'ready'}
						<Badge variant="outline">ready · {row.chunkCount} chunks</Badge>
					{:else}
						<Badge variant="destructive" title={row.error}>error</Badge>
					{/if}
				</div>
			{/each}
		</div>
	{/if}

	{#if isBusy}
		<p class="text-center text-xs text-muted-foreground">Working…</p>
	{/if}
</div>
