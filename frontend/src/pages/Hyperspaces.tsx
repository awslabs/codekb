import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Box from "@cloudscape-design/components/box";
import Modal from "@cloudscape-design/components/modal";
import Badge from "@cloudscape-design/components/badge";
import Alert from "@cloudscape-design/components/alert";
import Flashbar, { FlashbarProps } from "@cloudscape-design/components/flashbar";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import Link from "@cloudscape-design/components/link";
import Toggle from "@cloudscape-design/components/toggle";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, Hyperspace } from "../api/client";
import { getUser } from "../auth";

export default function Hyperspaces() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: hyperspaces = [], isLoading: loading, refetch: refetchHs } = useQuery({
    queryKey: ["hyperspaces"],
    queryFn: () => api.listHyperspaces(),
  });

  const [selectedItems, setSelectedItems] = useState<Hyperspace[]>([]);
  const [deleteVisible, setDeleteVisible] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flash, setFlash] = useState<FlashbarProps.MessageDefinition[]>([]);
  const [filterText, setFilterText] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  // "My hyperspaces" filter — opt-in, defaults OFF so teammates see all
  // hyperspaces (including ones a colleague created) on first load.
  const currentUser = getUser();
  const [showMyHyperspaces, setShowMyHyperspaces] = useState(false);

  const handleDelete = async () => {
    setDeleting(true);
    // Delete all selected independently so one denial doesn't abort the rest or
    // hide the deletes that already succeeded.
    const items = selectedItems;
    const results = await Promise.allSettled(
      items.map((item) => api.deleteHyperspace(item.hyperspace_id)),
    );
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === "rejected");
    const succeeded = items.length - failures.length;
    setDeleteVisible(false);
    setSelectedItems([]);
    queryClient.invalidateQueries({ queryKey: ["hyperspaces"] });
    if (failures.length === 0) {
      setFlash([{ type: "success", content: t("hyperspaces.flashDeleted", { count: succeeded }), dismissible: true, onDismiss: () => setFlash([]) }]);
    } else {
      const authz = failures
        .map((f) => f.reason)
        .find((r) => r instanceof ApiError && r.isAuthz) as ApiError | undefined;
      const detail = authz ? authz.displayMessage : failures[0].reason?.message ?? "";
      setFlash([{
        type: succeeded > 0 ? "warning" : "error",
        content: t("hyperspaces.flashDeletePartial", { succeeded, total: items.length, failed: failures.length, detail }),
        dismissible: true,
        onDismiss: () => setFlash([]),
      }]);
    }
    setDeleting(false);
  };

  const filtered = hyperspaces.filter(
    (hs) => {
      // "My hyperspaces" toggle: filter to current user's hyperspaces
      if (showMyHyperspaces) {
        const myIdentity = (currentUser?.email || "anonymous").toLowerCase();
        if ((hs.created_by ?? "").toLowerCase() !== myIdentity) return false;
      }
      return (
        !filterText ||
        hs.name.toLowerCase().includes(filterText.toLowerCase()) ||
        hs.hyperspace_id.toLowerCase().includes(filterText.toLowerCase())
      );
    },
  );

  const invalidCount = hyperspaces.filter((hs) => hs.invalid).length;

  const pagesCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const paginated = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <SpaceBetween size="l">
      <BreadcrumbGroup
        items={[
          { text: t("common.breadcrumbRoot"), href: "/" },
          { text: t("nav.hyperspaces"), href: "/hyperspaces" },
        ]}
        onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
      />
      <Flashbar items={flash} />

      {invalidCount > 0 && (
        <Alert type="warning" header={t("hyperspaces.invalidHeader")}>
          {t("hyperspaces.invalidBody", { count: invalidCount })}
        </Alert>
      )}

      <Table
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        loading={loading}
        loadingText={t("hyperspaces.loadingText")}
        items={paginated}
        trackBy="hyperspace_id"
        empty={
          <Box textAlign="center" padding="l" color="inherit">
            <b>{t("hyperspaces.emptyTitle")}</b>
            <Box padding={{ bottom: "s" }} variant="p" color="inherit">
              {t("hyperspaces.emptyBody")}
            </Box>
            <Button onClick={() => navigate("/hyperspaces/new")}>{t("hyperspaces.newHyperspace")}</Button>
          </Box>
        }
        header={
          <Header
            description={t("hyperspaces.description")}
            counter={`(${filtered.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" ariaLabel={t("common.refresh")} onClick={() => refetchHs()} />
                <Button disabled={selectedItems.length === 0} onClick={() => setDeleteVisible(true)}>
                  {t("hyperspaces.delete")}
                </Button>
                <Button variant="primary" onClick={() => navigate("/hyperspaces/new")}>
                  {t("hyperspaces.newHyperspace")}
                </Button>
              </SpaceBetween>
            }
          >
            {t("nav.hyperspaces")}
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder={t("hyperspaces.findPlaceholder")}
              onChange={({ detail }) => { setFilterText(detail.filteringText); setCurrentPage(1); }}
            />
            <Toggle
              checked={showMyHyperspaces}
              onChange={({ detail }) => { setShowMyHyperspaces(detail.checked); setCurrentPage(1); }}
            >
              {t("hyperspaces.myHyperspaces")}
            </Toggle>
          </SpaceBetween>
        }
        pagination={<Pagination currentPageIndex={currentPage} pagesCount={pagesCount} ariaLabels={{ nextPageLabel: t("hyperspaces.pageNext"), previousPageLabel: t("hyperspaces.pagePrevious"), pageLabel: (n) => t("hyperspaces.pageLabel", { page: n }) }} onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)} />}
        preferences={
          <CollectionPreferences
            title={t("common.tableSettings")}
            confirmLabel={t("common.confirm")}
            cancelLabel={t("common.cancel")}
            preferences={{ pageSize }}
            onConfirm={({ detail }) => {
              setPageSize(detail.pageSize ?? 10);
              setCurrentPage(1);
            }}
            pageSizePreference={{
              title: t("common.pageSize"),
              options: [
                { value: 10, label: t("common.rows", { count: 10 }) },
                { value: 25, label: t("common.rows", { count: 25 }) },
                { value: 50, label: t("common.rows", { count: 50 }) },
                { value: 100, label: t("common.rows", { count: 100 }) },
              ],
            }}
          />
        }
        columnDefinitions={[
          {
            id: "name",
            header: t("hyperspaces.colName"),
            cell: (item) => (
              <div>
                <SpaceBetween direction="horizontal" size="xs">
                  <Link
                    fontSize="body-m"
                    onFollow={(e) => { e.preventDefault(); navigate(`/hyperspaces/${item.hyperspace_id}`); }}
                    href={`/hyperspaces/${item.hyperspace_id}`}
                  >
                    <b>{item.name}</b>
                  </Link>
                  {item.invalid && <Badge color="severity-medium">{t("hyperspaces.badgeInvalid")}</Badge>}
                  {(item.inaccessible_space_ids?.length ?? 0) > 0 && (
                    <Badge color="grey">{t("hyperspaces.badgeNoAccess")}</Badge>
                  )}
                </SpaceBetween>
                <div style={{ fontSize: 11, color: "#687078", marginTop: 2 }}>{item.hyperspace_id}</div>
              </div>
            ),
            sortingField: "name",
          },
          {
            id: "spaces",
            header: t("hyperspaces.colSpaces"),
            cell: (item) => {
              const missing = new Set(item.missing_space_ids ?? []);
              const inaccessible = new Set(item.inaccessible_space_ids ?? []);
              const MAX_BADGES = 4;
              const shown = item.space_ids.slice(0, MAX_BADGES);
              const overflow = item.space_ids.length - shown.length;
              return (
                <SpaceBetween direction="horizontal" size="xs">
                  {shown.map((sid) =>
                    missing.has(sid) ? (
                      <Badge key={sid} color="severity-medium">{t("hyperspaces.spaceDeleted", { id: sid })}</Badge>
                    ) : inaccessible.has(sid) ? (
                      <Badge key={sid} color="grey">{t("hyperspaces.spaceNoAccess", { id: sid })}</Badge>
                    ) : (
                      <Badge key={sid} color="blue">{sid}</Badge>
                    ),
                  )}
                  {overflow > 0 && (
                    <Link
                      onFollow={(e) => { e.preventDefault(); navigate(`/hyperspaces/${item.hyperspace_id}`); }}
                      href={`/hyperspaces/${item.hyperspace_id}`}
                    >
                      {t("hyperspaces.more", { count: overflow })}
                    </Link>
                  )}
                </SpaceBetween>
              );
            },
          },
          {
            id: "count",
            header: t("hyperspaces.colCount"),
            cell: (item) => item.space_ids.length,
          },
          {
            id: "created_by",
            header: t("hyperspaces.colCreatedBy"),
            cell: (item) => item.created_by || "—",
          },
        ]}
      />

      {/* Delete confirmation */}
      <Modal
        visible={deleteVisible}
        onDismiss={() => setDeleteVisible(false)}
        header={t("hyperspaces.deleteHeader")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDeleteVisible(false)}>{t("common.cancel")}</Button>
              <Button variant="primary" loading={deleting} onClick={handleDelete}>
                {t("hyperspaces.delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {t("hyperspaces.deleteBody", { count: selectedItems.length })}
        <Box variant="p" padding={{ top: "s" }}>
          {selectedItems.map((hs) => (
            <Box key={hs.hyperspace_id} variant="code">{hs.name} ({hs.hyperspace_id})</Box>
          ))}
        </Box>
      </Modal>

    </SpaceBetween>
  );
}
