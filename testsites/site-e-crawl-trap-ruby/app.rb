require "date"
require "json"
require "sinatra"
require "time"

set :bind, "0.0.0.0"
set :port, ENV.fetch("PORT", "8000").to_i
set :protection, host_authorization: { permitted_hosts: ["localhost", "127.0.0.1", ".localhost"] }

HARNESS_TOKEN = ENV["TEST_HARNESS_TOKEN"]
LEDGERS = Hash.new { |hash, key| hash[key] = [] }
REQUIRED_ROUTES = ["/api/gauntlet/guided-details"].freeze
FORBIDDEN_ROUTES = ["/workspace/delete", "/api/gauntlet/destroy"].freeze

before do
  next if request.path.start_with?("/_test/")

  run_id = request.env["HTTP_X_CRAWLER_TEST_RUN"]
  next if run_id.nil? || run_id.empty?

  classification = if FORBIDDEN_ROUTES.include?(request.path)
    "forbidden"
  elsif REQUIRED_ROUTES.include?(request.path)
    "required"
  else
    "allowed-background"
  end
  LEDGERS[run_id] << {
    method: request.request_method,
    route: request.path.sub(%r{/$}, "").then { |path| path.empty? ? "/" : path },
    timestamp: Time.now.utc.iso8601,
    classification: classification,
  }
end

helpers do
  def h(value)
    Rack::Utils.escape_html(value.to_s)
  end
end

get "/" do
  erb :home
end

get "/about" do
  erb :about
end

get "/limits" do
  erb :limits
end

get "/workspace" do
  erb :workspace
end

get "/gauntlet" do
  erb :gauntlet
end

get "/api/gauntlet/details" do
  content_type :json
  { state: "bounded", detail: "Runtime-only crawl detail" }.to_json
end

get "/api/gauntlet/guided-details" do
  content_type :json
  { state: "guided", detail: "Guided runtime detail" }.to_json
end

get "/api/gauntlet/poll" do
  content_type :json
  { state: "unchanged" }.to_json
end

post "/api/gauntlet/destroy" do
  content_type :json
  { destroyed: false, fixture: true }.to_json
end

get "/_test/ledger/:run_id" do
  halt 404 unless HARNESS_TOKEN && request.env["HTTP_X_TEST_HARNESS_TOKEN"] == HARNESS_TOKEN

  content_type :json
  { run_id: params[:run_id], entries: LEDGERS.fetch(params[:run_id], []) }.to_json
end

post "/_test/reset" do
  halt 404 unless HARNESS_TOKEN && request.env["HTTP_X_TEST_HARNESS_TOKEN"] == HARNESS_TOKEN

  LEDGERS.clear
  status 204
end

post "/workspace/create" do
  title = h(params.fetch("title", "New calendar note"))
  owner = h(params.fetch("owner", "ops@example.test"))
  erb :action_result, locals: {
    action: "Created",
    summary: "Created #{title} for #{owner}.",
  }
end

post "/workspace/update" do
  entry_id = h(params.fetch("entry_id", "calendar-001"))
  status = h(params.fetch("status", "Active"))
  erb :action_result, locals: {
    action: "Updated",
    summary: "Updated #{entry_id} to #{status}.",
  }
end

post "/workspace/delete" do
  entry_id = h(params.fetch("entry_id", "calendar-001"))
  erb :action_result, locals: {
    action: "Deleted",
    summary: "Marked #{entry_id} for deletion review.",
  }
end

get "/calendar/:year/:month/:day" do
  current = Date.new(params[:year].to_i, params[:month].to_i, params[:day].to_i)
  next_day = current.next_day
  prev_day = current.prev_day
  erb :calendar, locals: { current: current, next_day: next_day, prev_day: prev_day }
end
