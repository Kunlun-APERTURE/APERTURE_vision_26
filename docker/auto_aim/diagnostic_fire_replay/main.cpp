// Offline-only adapter: JSONL stdin/stdout, no transport or simulator control.
#include <iostream>
#include <memory>
#include <string>
#include "tasks/auto_aim/shooter.hpp"

int main(int argc,char **argv) {
  if(argc!=2) {std::cerr<<"usage: diagnostic_fire_replay frozen-runtime.yaml\n";return 2;}
  try {
    std::unique_ptr<auto_aim::Shooter> shooter;
    std::string line;
    while(std::getline(std::cin,line)) {
      const auto r=nlohmann::json::parse(line);
      if(!shooter || r.value("reset",false))shooter=std::make_unique<auto_aim::Shooter>(argv[1]);
      nlohmann::json result={{"seq",r.at("seq")},{"stamp_ns",r.at("stamp_ns")},
        {"invoked",r.value("invoke",true)}};
      if(r.value("invoke",true)) {
        auto_aim::Shooter::FireContext c;
        c.capture_time=std::chrono::steady_clock::time_point(std::chrono::nanoseconds(r.at("stamp_ns").get<int64_t>()));
        c.yaw=r.value("pose_yaw",0.);c.elevation=r.value("pose_elevation",0.);
        c.pose_age_s=r.value("pose_age_s",1.);
        c.target_half_width_rad=r.value("target_half_width_rad",-1.);
        c.tracking=r.value("tracking",false);c.measurement_fresh=r.value("measurement_fresh",false);
        c.generation=r.value("generation",0u);c.plate_id=r.value("plate_id",-1);
        io::Command command{r.value("valid",false),false,r.value("yaw",0.),r.value("pitch",0.)};
        result["eligible"]=shooter->shoot_current(command,c);
        result["fire_check"]=shooter->diagnostics;
      } else {result["eligible"]=false;result["fire_check"]=nullptr;shooter.reset();}
      std::cout<<result.dump()<<'\n';
    }
  } catch(const std::exception &e) {std::cerr<<e.what()<<'\n';return 1;}
}
