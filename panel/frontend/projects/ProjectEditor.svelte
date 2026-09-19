<script lang="ts">
	// Create or edit a project. Everything a chat in the project inherits:
	// instructions, memories, working directory, model, permission mode.
	import { Button } from '$lib/components/ui/button';
	import * as Dialog from '$lib/components/ui/dialog';
	import { Input } from '$lib/components/ui/input';
	import { modelsStore } from '$lib/stores';
	import { toast, Trash2 } from '$lib/utils/panel-command-runtime';
	import { projectsStore } from './store.svelte';
	import type { Project, ProjectPermissionMode } from './types';

	interface Props {
		open?: boolean;
		project: Project | null;
	}

	let { open = $bindable(false), project }: Props = $props();

	const MODES: ProjectPermissionMode[] = ['manual', 'accept-edits', 'auto', 'plan'];

	let draftName = $state('');
	let draftInstructions = $state('');
	let draftCwd = $state('');
	let draftModel = $state('');
	let draftMode = $state<ProjectPermissionMode | ''>('');
	let newMemory = $state('');
	let saving = $state(false);

	const modelNames = $derived(
		((modelsStore.models ?? []) as { model: string }[]).map((m) => m.model)
	);

	// Load the project into the draft whenever the dialog opens, so editing an
	// existing project starts from its values and creating starts empty.
	$effect(() => {
		if (!open) return;

		draftName = project?.name ?? '';
		draftInstructions = project?.instructions ?? '';
		draftCwd = project?.cwd ?? '';
		draftModel = project?.model ?? '';
		draftMode = project?.permissionMode ?? '';
		newMemory = '';
	});

	async function save(): Promise<void> {
		const name = draftName.trim();

		if (!name) {
			toast.error('A project needs a name');

			return;
		}

		saving = true;

		try {
			const patch = {
				cwd: draftCwd.trim() || null,
				instructions: draftInstructions,
				model: draftModel.trim() || null,
				name,
				permissionMode: draftMode || null
			};

			if (project) {
				await projectsStore.updateProject(project.id, patch);
				toast.success(`Saved "${name}"`);
			} else {
				await projectsStore.createProject(name, patch);
				toast.success(`Created "${name}"`, {
					description: 'Its chats inherit these settings.'
				});
			}

			open = false;
		} catch (error) {
			toast.error('Could not save the project', {
				description: error instanceof Error ? error.message : String(error)
			});
		} finally {
			saving = false;
		}
	}

	async function addMemory(): Promise<void> {
		const text = newMemory.trim();

		if (!text) return;

		if (!project) {
			toast.error('Save the project first, then add memories');

			return;
		}

		await projectsStore.addMemory(project.id, text, null);
		newMemory = '';
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="flex max-h-[calc(100vh-4rem)] flex-col gap-3 overflow-y-auto sm:max-w-xl">
		<Dialog.Header>
			<Dialog.Title>{project ? 'Edit project' : 'New project'}</Dialog.Title>
		</Dialog.Header>

		<label class="flex flex-col gap-1">
			<span class="text-muted-foreground text-xs font-medium">Name</span>
			<Input bind:value={draftName} placeholder="Tokenizer work" />
		</label>

		<label class="flex flex-col gap-1">
			<span class="text-muted-foreground text-xs font-medium">Instructions</span>
			<textarea
				bind:value={draftInstructions}
				class="border-input bg-background placeholder:text-muted-foreground focus-visible:ring-ring min-h-24 w-full rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:outline-none"
				placeholder="What this project is about, conventions to follow, how you want answers."
			></textarea>
			<span class="text-muted-foreground/70 text-[11px]">
				Added to the system prompt of chats started in this project — not to your global system
				message, so other chats are untouched.
			</span>
		</label>

		<div class="grid gap-3 sm:grid-cols-2">
			<label class="flex flex-col gap-1">
				<span class="text-muted-foreground text-xs font-medium">Working directory</span>
				<Input bind:value={draftCwd} placeholder="/home/you/project" />
			</label>

			<label class="flex flex-col gap-1">
				<span class="text-muted-foreground text-xs font-medium">Model</span>
				<Input bind:value={draftModel} placeholder="model name" list="project-model-names" />
				<datalist id="project-model-names">
					{#each modelNames as modelName (modelName)}
						<option value={modelName}></option>
					{/each}
				</datalist>
			</label>
		</div>

		<div class="flex flex-col gap-1">
			<span class="text-muted-foreground text-xs font-medium">Permission mode</span>
			<div class="flex flex-wrap gap-1">
				<Button
					variant={draftMode === '' ? 'secondary' : 'ghost'}
					size="sm"
					class="h-7 text-[11px]"
					onclick={() => (draftMode = '')}
				>
					Leave as-is
				</Button>
				{#each MODES as mode (mode)}
					<Button
						variant={draftMode === mode ? 'secondary' : 'ghost'}
						size="sm"
						class="h-7 text-[11px]"
						onclick={() => (draftMode = mode)}
					>
						{mode}
					</Button>
				{/each}
			</div>
		</div>

		<div class="flex flex-col gap-1">
			<span class="text-muted-foreground text-xs font-medium">Memories</span>

			{#if project?.memories.length}
				<ul class="flex flex-col gap-1">
					{#each project.memories as memory (memory.id)}
						<li class="bg-muted/50 flex items-start gap-2 rounded-md px-2 py-1">
							<span class="min-w-0 flex-1 text-xs leading-relaxed">{memory.text}</span>
							<button
								type="button"
								class="text-muted-foreground hover:text-destructive shrink-0"
								title="Remove memory"
								aria-label="Remove memory"
								onclick={() => projectsStore.removeMemory(project!.id, memory.id)}
							>
								<Trash2 class="h-3 w-3" />
							</button>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="text-muted-foreground/70 text-[11px]">
					No memories yet. Memories are facts every chat in this project should know; the model
					will suggest some after a chat, and you approve them.
				</p>
			{/if}

			<div class="flex items-center gap-1">
				<Input bind:value={newMemory} placeholder="Add a memory…" />
				<Button
					variant="secondary"
					size="sm"
					class="h-8 shrink-0"
					disabled={!project}
					onclick={addMemory}
				>
					Add
				</Button>
			</div>
		</div>

		<div class="flex justify-end gap-2 pt-1">
			<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
			<Button disabled={saving} onclick={save}>{project ? 'Save' : 'Create'}</Button>
		</div>
	</Dialog.Content>
</Dialog.Root>
