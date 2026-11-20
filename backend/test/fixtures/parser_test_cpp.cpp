// C++ parser integration fixture.
// Exercises namespaces, classes, access sections, ctors/dtors, static,
// virtual/pure-virtual, operator overloads, templates, enums, free functions,
// out-of-line definitions, globals/constants, and calls.

#include <string>
#include <vector>

int global_counter = 0;
constexpr double PI = 3.14159;

namespace app {

// Abstract base with a pure virtual method.
class Repository {
public:
    virtual bool save(const std::string& id) = 0;
    virtual ~Repository();
};

class UserService : public Repository {
public:
    UserService();
    explicit UserService(int capacity);
    ~UserService() override;

    bool save(const std::string& id) override;
    void addUser(const std::string& name, int age = 18);
    static int version();
    UserService& operator=(const UserService& other);

protected:
    void notifyAdmin();

private:
    int helperCount();
    int count_;
    static const int MAX_USERS = 100;
};

template <typename T>
class Box {
public:
    T get() const;
    void set(T value);
};

enum class Role { Admin, Guest };

void freeHelper(int a, int b);

} // namespace app

// ---- out-of-line definitions (in the same TU for the fixture) ----

app::UserService::UserService() {
    count_ = 0;
}

bool app::UserService::save(const std::string& id) {
    notifyAdmin();
    return true;
}

void app::UserService::addUser(const std::string& name, int age) {
    UserService svc;
    Repository* repo = new UserService();
    svc.save(name);
    repo->save(name);
    freeHelper(age, count_);
}

void app::UserService::notifyAdmin() {
    version();
}

int app::UserService::version() {
    return 1;
}

void app::freeHelper(int a, int b) {
    global_counter = a + b;
}
