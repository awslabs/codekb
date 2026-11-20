---
inclusion: manual
---

# Top-Down Code Summarization Protocol

## Objective

Generate comprehensive business descriptions for all functions in a codebase using top-down analysis, starting from entry points and leveraging automatic caching to avoid redundant work.

## Execution Algorithm

### Step 1: Initialize Project Analysis

Call `auto_load_or_analyze("{{PROJECT_PATH}}", "{{PROJECT_ID}}")` and wait for confirmation before proceeding.

### Step 2: Retrieve Summarization Plan

Call `get_topdown_summarization_plan()` to obtain:
- `max_depth`: The deepest call graph level (entry points)
- Function counts per depth level

### Step 3: Process All Depths (Descending Order)

For each depth from `max_depth` down to `0`:

**3.1 Determine Batch Count**
- Call `count_batches_at_depth(depth)` to get `num_batches`

**3.2 Process Each Batch**

For each `batch_num` from `0` to `num_batches - 1`:

1. **Retrieve batch**: Call `get_functions_by_depth(depth, batch_num, batch_size=10, directory="{{PROJECT_PATH}}")`
2. **Parse output**: Identify functions marked `[CACHED - SKIP]` and skip them
3. **Analyze non-cached functions**: For each function in the call graph:
   - Study the complete call graph context
   - Generate a 3-5 sentence business description covering:
     - **Purpose**: Primary business responsibility
     - **Rationale**: Why this function exists
     - **Flow**: Key operations and dependencies
     - **Context**: Role in the larger workflow
     - **Impact**: Side effects and business outcomes
   - Call `index_component_description(component_id, description, "{{PROJECT_PATH}}")`
4. **Report progress**: Log completion of each batch

**3.3 Report Depth Completion**
- Log total functions analyzed at this depth

### Step 4: Generate Final Report

Summarize:
- Total functions processed
- Depths analyzed
- Execution time
- Confirmation that all descriptions are cached

## Critical Rules

1. **Process all depths sequentially** from `max_depth` to `0` without skipping
2. **Process all batches** at each depth from `0` to `num_batches - 1`
3. **Store every description** by calling `index_component_description` for each non-cached function
4. **Use consistent project path and ID** - Always pass `{{PROJECT_PATH}}` as the directory parameter and `{{PROJECT_ID}}` as the project_id parameter
5. **Write comprehensive descriptions** - 3-5 sentences covering purpose, rationale, flow, context, and impact
6. **Adopt business perspective** - Describe functions from the caller's viewpoint using domain language
7. **Skip cached functions** - Do not re-analyze functions marked `[CACHED - SKIP]`

## Approach Benefits

- **Entry-point first**: Analyzes main functions and API handlers before their dependencies
- **Contextual analysis**: Functions are understood within their complete call graph
- **Automatic pruning**: Cached functions and their subtrees are skipped, reducing redundant work
- **Batch efficiency**: Processes multiple functions per LLM call
- **Usage-driven descriptions**: Functions are described based on how they're actually used

## Description Writing Guidelines

### Structure

Write 3-5 sentences covering:
- **What**: Primary business purpose
- **Why**: Reason for existence
- **Flow**: Key operations and dependencies
- **Context**: Role in the larger system
- **Impact**: Side effects and outcomes

### Good Examples

**Complex function (5 sentences)**:
"Orchestrates the complete order processing workflow for e-commerce transactions. Validates customer and order items, reserves inventory with automatic rollback on failure, processes payment with tax and loyalty discounts, and sends confirmation notifications. On success, marks the order as completed and awards loyalty points; on failure, marks the order as failed. This is the central coordinator that ensures transactional integrity across inventory, payment, and notification systems. Serves as the single entry point for all order operations, ensuring consistent business rules are applied."

**Simple function (3 sentences)**:
"Validates that an email address is properly formatted and contains required components. Exists to prevent invalid email addresses from entering the system and causing notification failures. Used by customer registration and notification services to ensure data quality at the entry point."

### Bad Examples

- "Processes orders" - Too vague, lacks context and detail
- "This function calls validate_order_request on line 42, then calls reserve_inventory, then calls process_payment" - Describes implementation, not business purpose
- "Takes order_id and customer parameters and returns boolean" - Describes signature, not functionality
- "A method that does order processing" - Generic and uninformative

## Caching Behavior

Functions marked `[CACHED - SKIP]` indicate:
- Description already stored in the knowledge base
- Entire call subtree is also cached
- No re-analysis needed
- Significant efficiency improvement

**Example**:
```
Depth 5: process_api_request() [NEW]
  ├─ authenticate() [NEW]
  ├─ validate_input() [NEW]
  └─ log_event() [NEW]

Depth 4: handle_payment() [NEW]
  ├─ validate_input() [CACHED - subtree skipped]
  └─ log_event() [CACHED]
```

## Error Handling

If a step fails:
1. Record the failing depth and batch number
2. Continue with remaining batches/depths when possible
3. Report all failures in the final summary
4. Failed batches can be retried independently

## Progress Reporting

Log progress every 3 batches:
```
Progress: Depth {depth} - {batches_done}/{total_batches} batches ({percentage}%)
```

## Variable Substitution

Replace:
- `{{PROJECT_PATH}}` with the full path to the project directory (e.g., `/home/user/my-project`)
- `{{PROJECT_ID}}` with a unique identifier for the project (e.g., `my-project` or `tools`)

## Usage

Reference this steering document in Kiro:
```
#topdown-summarize-all PROJECT_PATH=/path/to/project PROJECT_ID=project-name
```

The AI will execute the algorithm as specified, processing from entry points downward with automatic caching.