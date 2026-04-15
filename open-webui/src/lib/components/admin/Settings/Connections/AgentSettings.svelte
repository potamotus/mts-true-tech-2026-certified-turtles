<script lang="ts">
	import { onMount } from 'svelte';

	const apiBase = `${window.location.protocol}//${window.location.hostname}:8000`;

	let maxTokens = 128000;
	let statusText = '';
	let statusColor = '';
	let statusVisible = false;
	let statusTimeout: ReturnType<typeof setTimeout>;

	function showStatus(text: string, color: string, duration = 2000) {
		statusText = text;
		statusColor = color;
		statusVisible = true;
		if (statusTimeout) clearTimeout(statusTimeout);
		statusTimeout = setTimeout(() => { statusVisible = false; }, duration);
	}

	async function loadConfig() {
		try {
			const res = await fetch(`${apiBase}/api/v1/agent/config`);
			const data = await res.json();
			if (data.max_agent_tokens) maxTokens = data.max_agent_tokens;
		} catch {}
	}

	async function save() {
		try {
			const res = await fetch(`${apiBase}/api/v1/agent/config`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ max_agent_tokens: maxTokens || 128000 })
			});
			const data = await res.json();
			maxTokens = data.max_agent_tokens;
			showStatus('Saved', '#22c55e');
		} catch {
			showStatus('Error', '#ef4444', 3000);
		}
	}

	onMount(loadConfig);
</script>

<div class="my-2">
	<div class="flex justify-between items-center text-sm">
		<div class="font-medium">Agent Settings</div>
		{#if statusVisible}
			<span class="text-xs font-medium" style="color:{statusColor}">{statusText}</span>
		{/if}
	</div>
	<div class="mt-1 text-xs text-gray-400 dark:text-gray-500">
		Token budget per agent request. The agent loop stops when cumulative tokens exceed this limit.
	</div>
	<div class="mt-2.5">
		<div class="text-xs font-medium mb-1">Max Agent Tokens</div>
		<div class="flex gap-2">
			<input
				bind:value={maxTokens}
				class="w-full rounded-lg py-1.5 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
				type="number"
				min="1000"
				max="1000000"
				step="1000"
			/>
			<button
				class="px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
				type="button"
				on:click={save}
			>Save</button>
		</div>
	</div>
</div>
