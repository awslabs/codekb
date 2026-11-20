import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Table from "@cloudscape-design/components/table";
import Badge from "@cloudscape-design/components/badge";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import Toggle from "@cloudscape-design/components/toggle";
import { api, isQueryable, SpaceSummary } from "../api/client";
import { getUser } from "../auth";

const MIN_SPACES = 1;

export default function CreateHyperspace() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: spaces = [] } = useQuery({
    queryKey: ["spaces"],
    queryFn: () => api.listSpaces(),
  });

  // "My spaces" filter — opt-in, defaults OFF so you can pick from all spaces
  // (including ones colleagues created) when building a hyperspace.
  const currentUser = getUser();
  const [showMySpaces, setShowMySpaces] = useState(false);

  const [name, setName] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);

  const [selSelection, setSelSelection] = useState<SpaceSummary[]>([]);
  const [selFilter, setSelFilter] = useState("");
  const [selPage, setSelPage] = useState(1);
  const [addSelection, setAddSelection] = useState<SpaceSummary[]>([]);
  const [addFilter, setAddFilter] = useState("");
  const [addPage, setAddPage] = useState(1);
  const pageSize = 10;

  const readySpaces = useMemo(() => spaces.filter((s) => isQueryable(s.status)), [spaces]);
  const selectedSpaces = useMemo(
    () => selectedIds.map((id) => readySpaces.find((s) => s.space_id === id)).filter(Boolean) as SpaceSummary[],
    [selectedIds, readySpaces],
  );
  const availableSpaces = useMemo(
    () =>
      readySpaces.filter((s) => {
        if (selectedIds.includes(s.space_id)) return false;
        // "My spaces" toggle: limit the pool to the current user's own spaces.
        if (showMySpaces) {
          const myIdentity = (currentUser?.email || "anonymous").toLowerCase();
          if ((s.created_by ?? "").toLowerCase() !== myIdentity) return false;
        }
        return true;
      }),
    [readySpaces, selectedIds, showMySpaces, currentUser],
  );

  const filterFn = (list: SpaceSummary[], q: string) => {
    const t = q.trim().toLowerCase();
    if (!t) return list;
    return list.filter(
      (s) => s.space_id.toLowerCase().includes(t) || (s.space_name ?? "").toLowerCase().includes(t),
    );
  };
  const filteredSelected = useMemo(() => filterFn(selectedSpaces, selFilter), [selectedSpaces, selFilter]);
  const filteredAvailable = useMemo(() => filterFn(availableSpaces, addFilter), [availableSpaces, addFilter]);
  const selPageItems = filteredSelected.slice((selPage - 1) * pageSize, selPage * pageSize);
  const addPageItems = filteredAvailable.slice((addPage - 1) * pageSize, addPage * pageSize);

  const addSpaces = (toAdd: SpaceSummary[]) => {
    setSelectedIds((prev) => {
      const existing = new Set(prev);
      return [...prev, ...toAdd.map((s) => s.space_id).filter((id) => !existing.has(id))];
    });
    setAddSelection([]);
  };
  const removeSpaces = (ids: string[]) => {
    const drop = new Set(ids);
    setSelectedIds((prev) => prev.filter((id) => !drop.has(id)));
    setSelSelection([]);
  };

  const isValid = !!name.trim() && selectedIds.length >= MIN_SPACES;

  const handleCreate = async () => {
    if (!isValid) return;
    setSubmitting(true);
    setFlash([]);
    try {
      const created = await api.createHyperspace(name.trim(), selectedIds);
      queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
      navigate(created?.hyperspace_id ? `/hyperspaces/${created.hyperspace_id}` : "/hyperspaces");
    } catch (e: any) {
      setFlash([
        {
          type: "error",
          content: e.message || "Failed to create hyperspace",
          dismissible: true,
          onDismiss: () => setFlash([]),
        },
      ]);
      setSubmitting(false);
    }
  };

  const nameCell = (s: SpaceSummary) => (
    <div>
      <Box fontWeight="bold">{s.space_name || s.space_id}</Box>
      <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{s.space_id}</div>
    </div>
  );
  const langCell = (s: SpaceSummary) =>
    s.languages && s.languages.length > 0 ? (
      <SpaceBetween direction="horizontal" size="xxs">
        {s.languages.map((l) => (
          <Badge key={l} color="blue">
            {l}
          </Badge>
        ))}
      </SpaceBetween>
    ) : (
      "—"
    );

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: "CodeKB", href: "/" },
          { text: "Hyperspaces", href: "/hyperspaces" },
          { text: "Create", href: "/hyperspaces/new" },
        ]}
        onFollow={(e) => {
          e.preventDefault();
          navigate(e.detail.href);
        }}
      />
      <Flashbar items={flash} />

      <Form
        header={
          <Header variant="h1" description="Group 1 or more spaces together for cross-project analysis">
            Create hyperspace
          </Header>
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => navigate("/hyperspaces")}>
              Cancel
            </Button>
            <Button variant="primary" loading={submitting} disabled={!isValid} onClick={handleCreate}>
              Create
            </Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="l">
          <Container header={<Header variant="h2">Details</Header>}>
            <FormField label="Name" description="A descriptive name for this hyperspace">
              <Input
                value={name}
                onChange={({ detail }) => setName(detail.value)}
                placeholder="e.g. Payment Platform"
              />
            </FormField>
          </Container>

          {/* Selected spaces */}
          <Table
            selectionType="multi"
            selectedItems={selSelection}
            onSelectionChange={({ detail }) => setSelSelection(detail.selectedItems)}
            items={selPageItems}
            trackBy="space_id"
            variant="container"
            empty={
              <Box textAlign="center" padding="m" color="inherit">
                No spaces selected yet. Select one or more below and choose <b>Add selected</b>.
              </Box>
            }
            header={
              <Header
                counter={`(${selectedSpaces.length})`}
                description="Spaces that will be grouped into this hyperspace (at least one)"
                actions={
                  <Button
                    disabled={selSelection.length === 0}
                    onClick={() => removeSpaces(selSelection.map((s) => s.space_id))}
                  >
                    Remove selected
                  </Button>
                }
              >
                Selected spaces
              </Header>
            }
            filter={
              <TextFilter
                filteringText={selFilter}
                filteringPlaceholder="Find selected spaces"
                onChange={({ detail }) => {
                  setSelFilter(detail.filteringText);
                  setSelPage(1);
                }}
              />
            }
            pagination={
              <Pagination
                currentPageIndex={selPage}
                pagesCount={Math.max(1, Math.ceil(filteredSelected.length / pageSize))}
                onChange={({ detail }) => setSelPage(detail.currentPageIndex)}
              />
            }
            columnDefinitions={[
              { id: "name", header: "Name", cell: nameCell },
              { id: "components", header: "Components", cell: (s) => s.component_count ?? "—" },
              { id: "languages", header: "Languages", cell: langCell },
              {
                id: "actions",
                header: "",
                cell: (s) => (
                  <Button variant="inline-link" onClick={() => removeSpaces([s.space_id])}>
                    Remove
                  </Button>
                ),
              },
            ]}
          />

          {/* Available spaces */}
          <Table
            selectionType="multi"
            selectedItems={addSelection}
            onSelectionChange={({ detail }) => setAddSelection(detail.selectedItems)}
            items={addPageItems}
            trackBy="space_id"
            variant="container"
            empty={
              <Box textAlign="center" padding="m" color="inherit">
                {showMySpaces
                  ? 'None of your indexed spaces are available to add. Toggle off "My spaces" to add spaces created by others.'
                  : "No other indexed spaces available to add."}
              </Box>
            }
            header={
              <Header
                counter={`(${availableSpaces.length})`}
                description="Indexed spaces not yet selected"
                actions={
                  <Button disabled={addSelection.length === 0} onClick={() => addSpaces(addSelection)}>
                    Add selected
                  </Button>
                }
              >
                Available spaces
              </Header>
            }
            filter={
              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <TextFilter
                  filteringText={addFilter}
                  filteringPlaceholder="Find spaces to add"
                  onChange={({ detail }) => {
                    setAddFilter(detail.filteringText);
                    setAddPage(1);
                  }}
                />
                <Toggle
                  checked={showMySpaces}
                  onChange={({ detail }) => {
                    setShowMySpaces(detail.checked);
                    setAddPage(1);
                  }}
                >
                  My spaces
                </Toggle>
              </SpaceBetween>
            }
            pagination={
              <Pagination
                currentPageIndex={addPage}
                pagesCount={Math.max(1, Math.ceil(filteredAvailable.length / pageSize))}
                onChange={({ detail }) => setAddPage(detail.currentPageIndex)}
              />
            }
            columnDefinitions={[
              { id: "name", header: "Name", cell: nameCell },
              { id: "components", header: "Components", cell: (s) => s.component_count ?? "—" },
              { id: "languages", header: "Languages", cell: langCell },
              {
                id: "actions",
                header: "",
                cell: (s) => (
                  <Button variant="inline-link" onClick={() => addSpaces([s])}>
                    Add
                  </Button>
                ),
              },
            ]}
          />
        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}
